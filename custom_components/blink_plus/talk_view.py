"""Two-way talk for Home Assistant: the HTTP endpoint and the play_audio action.

The action is a plain domain service, not an entity service: entity services
of this platform run one at a time (``PARALLEL_UPDATES = 1``), so a running
``camera.record`` would hold the speaker back until it finished.

HTTP endpoint:

``POST /api/blink_plus/talk/<camera entity id>`` with a body of ADTS AAC frames
(AAC-LC, 16 kHz, mono), sent as a stream or as a whole file. Needs a Home
Assistant login with control permission for the camera, and an open live view.

    ffmpeg -i mic.wav -ac 1 -ar 16000 -c:a aac -b:a 32k -f adts - | \\
      curl -H "Authorization: Bearer $TOKEN" --data-binary @- \\
        http://homeassistant.local:8123/api/blink_plus/talk/camera.front_door
"""

from __future__ import annotations

from contextlib import aclosing
import os

from aiohttp import ClientError, web

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import service

from .const import DOMAIN
from .immis import BlinkLiveViewServer, TalkError
from .talk import (
    FRAME_SECONDS,
    AudioFormatError,
    TalkBusyError,
    async_talk,
    ffmpeg_adts,
)


def find_live_server(hass: HomeAssistant, entity_id: str) -> BlinkLiveViewServer | None:
    """Return the live view server of a Blink Plus camera entity."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        for server in entry.runtime_data.live_servers.values():
            if server.entity_id == entity_id:
                return server
    return None


class BlinkTalkView(HomeAssistantView):
    """Speak ADTS AAC audio through a camera's open live view."""

    url = "/api/blink_plus/talk/{entity_id}"
    name = "api:blink_plus:talk"
    requires_auth = True

    async def post(self, request: web.Request, entity_id: str) -> web.Response:
        """Stream the request body to the camera speaker."""
        hass = request.app[KEY_HASS]
        user = request["hass_user"]
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise web.HTTPForbidden
        server = find_live_server(hass, entity_id)
        if server is None:
            raise web.HTTPNotFound(text="Unknown Blink Plus camera")
        try:
            sent = await async_talk(server, request.content.iter_chunked(8192))
        except TalkBusyError as err:
            raise web.HTTPConflict(text=str(err)) from None
        except AudioFormatError as err:
            raise web.HTTPBadRequest(text=str(err)) from None
        except TimeoutError:
            raise web.HTTPRequestTimeout(text="Talk exceeded its time limit") from None
        except TalkError as err:
            raise web.HTTPServiceUnavailable(text=str(err)) from None
        except (ConnectionError, ClientError):
            # The client went away mid-upload; the speaker is already off.
            raise web.HTTPBadRequest(text="Upload interrupted") from None
        return self.json({"frames": sent, "seconds": round(sent * FRAME_SECONDS, 2)})


async def async_handle_play_audio(hass: HomeAssistant, call: ServiceCall) -> None:
    """blink_plus.play_audio: play a file on each targeted camera's speaker."""
    entity_ids = await service.async_extract_entity_ids(hass, call)
    if call.context.user_id:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None:
            raise UnknownUser(context=call.context)
        for entity_id in entity_ids:
            if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
                raise Unauthorized(
                    context=call.context, entity_id=entity_id, permission=POLICY_CONTROL
                )
    servers = [
        server
        for entity_id in sorted(entity_ids)
        if (server := find_live_server(hass, entity_id)) is not None
    ]
    if not servers:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="talk_no_camera"
        )
    for server in servers:
        await async_play_audio(hass, server, call.data["file_path"])


async def async_play_audio(
    hass: HomeAssistant, server: BlinkLiveViewServer, file_path: str
) -> None:
    """Play an audio file through one camera's speaker (open live view only)."""
    if not hass.config.is_allowed_path(file_path):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_path",
            translation_placeholders={"target": file_path},
        )
    if not await hass.async_add_executor_job(os.path.isfile, file_path):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="talk_no_file",
            translation_placeholders={"target": file_path},
        )
    if not server.talk_ready:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="talk_no_live_view"
        )
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415

        binary = get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001
        binary = "ffmpeg"
    try:
        async with aclosing(ffmpeg_adts(file_path, ffmpeg=binary)) as chunks:
            await async_talk(server, chunks)
    except TalkBusyError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="talk_busy"
        ) from err
    except AudioFormatError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="talk_bad_audio",
            translation_placeholders={"error": str(err)},
        ) from err
    except TalkError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="talk_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    except TimeoutError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="talk_too_long"
        ) from err
