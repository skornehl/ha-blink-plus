"""Config flow to configure Blink."""

from collections.abc import Mapping
import logging
from typing import Any, override

from blinkpy.auth import Auth, BlinkTwoFARequiredError, LoginError, TokenRefreshFailed
from aiohttp import ClientError
from blinkpy.blinkpy import Blink, BlinkSetupError
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_PIN, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .auth import (
    BlinkAuthStepError,
    BlinkBadCredentials,
    BlinkPlusAuth,
    BlinkRateLimited,
)
from .session import async_blink_session

from .const import (
    CONF_ARCHIVE_FOLDER,
    CONF_ARCHIVE_HOURS,
    CONF_ARCHIVE_INTERVAL,
    CONF_STREAM_BASE_PORT,
    CONF_STREAM_HOST,
    CONF_STREAM_LINGER,
    CONF_STREAM_MAX_MINUTES,
    CONF_STREAM_TOKEN_MODE,
    CONF_USB_DELETE_DAYS,
    CONF_USB_MOTION_MINUTES,
    DEFAULT_ARCHIVE_FOLDER,
    DEFAULT_ARCHIVE_HOURS,
    DEFAULT_ARCHIVE_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STREAM_BASE_PORT,
    DEFAULT_STREAM_HOST,
    DEFAULT_STREAM_LINGER,
    DEFAULT_STREAM_MAX_MINUTES,
    DEFAULT_STREAM_TOKEN_MODE,
    DEFAULT_USB_DELETE_DAYS,
    DEFAULT_USB_MOTION_MINUTES,
    DOMAIN,
    LEGACY_HARDWARE_ID,
)

_LOGGER = logging.getLogger(__name__)


async def validate_input(blink: Blink) -> None:
    """Log in (raises our detailed errors), then load the account."""
    await blink.auth.startup()  # BlinkTwoFARequiredError / BlinkAuthStepError...
    if await blink.start() is False:
        raise BlinkAuthStepError("loading cameras", None, "login worked, setup failed")


async def _send_blink_2fa_pin(blink: Blink, pin: str | None) -> None:
    """Send 2FA pin to blink servers."""
    if not pin:
        raise BlinkBadCredentials("2fa", None, "empty code")
    if not await blink.send_2fa_code(pin.strip()):
        raise BlinkAuthStepError("loading cameras", None, "2FA ok, setup failed")


def _error_for(err: Exception) -> tuple[str, str]:
    """Map a login exception to (form error key, detail text)."""
    if isinstance(err, BlinkRateLimited):
        return "rate_limited", str(err)
    if isinstance(err, BlinkBadCredentials):
        return ("invalid_2fa" if err.step == "2fa" else "invalid_auth"), str(err)
    if isinstance(err, (BlinkAuthStepError, LoginError, TokenRefreshFailed)):
        return "blink_error", str(err) or type(err).__name__
    if isinstance(err, (ClientError, TimeoutError, BlinkSetupError)):
        return "cannot_connect", str(err) or type(err).__name__
    _LOGGER.exception("Unexpected Blink login error")
    return "unknown", repr(err)


class BlinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Blink config flow."""

    VERSION = 5

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return BlinkOptionsFlow()

    def __init__(self) -> None:
        """Initialize the blink flow."""
        self.auth: Auth | None = None
        self.blink: Blink | None = None
        self._detail = ""

    def _existing_hardware_id(self) -> str | None:
        """Return the stored hardware_id for a reauth/reconfigure, or None if legacy or absent."""
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
        elif self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
        else:
            return None
        hardware_id = entry.data.get("hardware_id")
        if hardware_id and hardware_id != LEGACY_HARDWARE_ID:
            return hardware_id
        return None

    async def _handle_user_input(self, user_input: dict[str, Any]):
        """Handle user input."""
        # Omit hardware_id on new installs so blinkpy mints a per-install UUID;
        # reuse a stored non-legacy id so device identity stays stable on reauth.
        login_data: dict[str, Any] = {**user_input}
        login_data[CONF_USERNAME] = user_input[CONF_USERNAME].strip().lower()
        if "@" not in login_data[CONF_USERNAME]:
            raise InvalidEmail
        if hardware_id := self._existing_hardware_id():
            login_data["hardware_id"] = hardware_id
        session = async_blink_session(self.hass)
        self.auth = BlinkPlusAuth(login_data, no_prompt=True, session=session)
        self.blink = Blink(session=session)
        self.blink.auth = self.auth
        await self.async_set_unique_id(login_data[CONF_USERNAME])
        if self.source not in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            self._abort_if_unique_id_configured()

        await validate_input(self.blink)
        return self._async_finish_flow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        errors = {}
        if user_input is not None:
            try:
                return await self._handle_user_input(user_input)
            except BlinkTwoFARequiredError:
                return await self.async_step_2fa()
            except InvalidEmail:
                errors[CONF_USERNAME] = "invalid_email"
            except Exception as err:  # noqa: BLE001
                errors["base"], self._detail = _error_for(err)
                _LOGGER.warning("Blink login failed: %s", self._detail)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={"detail": self._detail},
        )

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle 2FA step."""
        errors = {}
        if user_input is not None:
            try:
                await _send_blink_2fa_pin(self.blink, user_input.get(CONF_PIN))
            except Exception as err:  # noqa: BLE001
                errors["base"], self._detail = _error_for(err)
                _LOGGER.warning("Blink 2FA failed: %s", self._detail)
            else:
                return self._async_finish_flow()

        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PIN): str
                }
            ),
            errors=errors,
            description_placeholders={"detail": self._detail},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth after an authentication error."""
        return await self.async_step_reauth_confirm(None)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors = {}
        if user_input is not None:
            try:
                return await self._handle_user_input(user_input)
            except BlinkTwoFARequiredError:
                return await self.async_step_2fa()
            except InvalidEmail:
                errors[CONF_USERNAME] = "invalid_email"
            except Exception as err:  # noqa: BLE001
                errors["base"], self._detail = _error_for(err)
                _LOGGER.warning("Blink login failed: %s", self._detail)

        config_entry = self._get_reauth_entry()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=config_entry.data[CONF_USERNAME]
                    ): str,
                    vol.Required(
                        CONF_PASSWORD,
                        default=config_entry.data[CONF_PASSWORD],
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "username": config_entry.data[CONF_USERNAME],
                "detail": self._detail,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration initiated by the user."""
        errors = {}
        if user_input is not None:
            try:
                return await self._handle_user_input(user_input)
            except BlinkTwoFARequiredError:
                return await self.async_step_2fa()
            except InvalidEmail:
                errors[CONF_USERNAME] = "invalid_email"
            except Exception as err:  # noqa: BLE001
                errors["base"], self._detail = _error_for(err)
                _LOGGER.warning("Blink login failed: %s", self._detail)

        config_entry = self._get_reconfigure_entry()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=config_entry.data[CONF_USERNAME]
                    ): str,
                    vol.Required(
                        CONF_PASSWORD,
                        default=config_entry.data[CONF_PASSWORD],
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={"detail": self._detail},
        )

    @callback
    def _async_finish_flow(self) -> ConfigFlowResult:
        """Finish with setup."""
        assert self.auth

        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            return self.async_update_reload_and_abort(
                self._get_reauth_entry()
                if self.source == SOURCE_REAUTH
                else self._get_reconfigure_entry(),
                data_updates=self.auth.login_attributes,
            )

        return self.async_create_entry(
            title=f"Blink Plus ({self.unique_id})", data=self.auth.login_attributes
        )


class BlinkOptionsFlow(OptionsFlow):
    """Polling and live view options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            user_input[CONF_ARCHIVE_FOLDER] = (
                user_input.get(CONF_ARCHIVE_FOLDER) or ""
            ).strip()
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=30)),
                    vol.Required(
                        CONF_STREAM_HOST,
                        default=opts.get(CONF_STREAM_HOST, DEFAULT_STREAM_HOST),
                    ): vol.In(["127.0.0.1", "0.0.0.0"]),
                    vol.Required(
                        CONF_STREAM_BASE_PORT,
                        default=opts.get(CONF_STREAM_BASE_PORT, DEFAULT_STREAM_BASE_PORT),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=65000)),
                    vol.Required(
                        CONF_STREAM_MAX_MINUTES,
                        default=opts.get(
                            CONF_STREAM_MAX_MINUTES, DEFAULT_STREAM_MAX_MINUTES
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=240)),
                    vol.Required(
                        CONF_STREAM_LINGER,
                        default=opts.get(CONF_STREAM_LINGER, DEFAULT_STREAM_LINGER),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=120)),
                    vol.Required(
                        CONF_STREAM_TOKEN_MODE,
                        default=opts.get(
                            CONF_STREAM_TOKEN_MODE, DEFAULT_STREAM_TOKEN_MODE
                        ),
                    ): vol.In(["auto", "token", "null"]),
                    vol.Required(
                        CONF_USB_MOTION_MINUTES,
                        default=opts.get(
                            CONF_USB_MOTION_MINUTES, DEFAULT_USB_MOTION_MINUTES
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
                    vol.Optional(
                        CONF_ARCHIVE_FOLDER,
                        description={
                            "suggested_value": opts.get(
                                CONF_ARCHIVE_FOLDER, DEFAULT_ARCHIVE_FOLDER
                            )
                        },
                    ): str,
                    vol.Required(
                        CONF_ARCHIVE_INTERVAL,
                        default=opts.get(CONF_ARCHIVE_INTERVAL, DEFAULT_ARCHIVE_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=10080)),
                    vol.Required(
                        CONF_ARCHIVE_HOURS,
                        default=opts.get(CONF_ARCHIVE_HOURS, DEFAULT_ARCHIVE_HOURS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=8760)),
                    vol.Required(
                        CONF_USB_DELETE_DAYS,
                        default=opts.get(CONF_USB_DELETE_DAYS, DEFAULT_USB_DELETE_DAYS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3650)),
                }
            ),
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class InvalidEmail(HomeAssistantError):
    """Username is not an email address."""
