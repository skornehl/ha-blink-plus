"""HTTP endpoint for two-way talk.

``POST /api/blink_plus/talk/<camera entity id>`` with a body of ADTS AAC frames
(AAC-LC, 16 kHz, mono), sent as a stream or as a whole file. Needs a Home
Assistant login with control permission for the camera, and an open live view.

    ffmpeg -i mic.wav -ac 1 -ar 16000 -c:a aac -b:a 32k -f adts - | \\
      curl -H "Authorization: Bearer $TOKEN" --data-binary @- \\
        http://homeassistant.local:8123/api/blink_plus/talk/camera.front_door
"""

from __future__ import annotations

from aiohttp import ClientError, web

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .immis import BlinkLiveViewServer, TalkError
from .talk import FRAME_SECONDS, AudioFormatError, TalkBusyError, async_talk


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
