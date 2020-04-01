"""
Fire an event when an API route is registered.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/remote_homeassistant/
"""
import logging
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.auth import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

ATTR_METHOD = 'method'
ATTR_ROUTE = 'route'
ATTR_AUTH_REQUIRED = 'auth_required'
ATTR_INSTANCE_NAME = 'instance_name'
ATTR_INSTANCE_IP = 'instance_ip'
ATTR_INSTANCE_PORT = 'instance_port'
ATTR_TOKEN = 'token'
ATTR_EVENT_DATA = "event_data"
EVENT_TYPE_REQUEST_ROUTES = 'request_routes'
EVENT_TYPE_ROUTE_REGISTERED = 'route_registered'

CONF_COMPONENTS = 'components'

DOMAIN = 'view_event'

CLUSTER_TOKEN_NAME = 'internal_cluster'

SCHEMA_REQUEST_ROUTES = \
    websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend({
        'type': EVENT_TYPE_REQUEST_ROUTES
    })

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_COMPONENTS): vol.All(cv.ensure_list,
                                               [cv.slugify])
    }),
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass, config):
    """Set up the view_event component."""

    user_owner = await hass.auth.async_get_owner()
    if not user_owner:
        _LOGGER.error('No admin user set up')
        return False

    for token in [token for token in user_owner.refresh_tokens.values() if
                  token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN and
                  token.client_name == CLUSTER_TOKEN_NAME]:
        await hass.auth.async_remove_refresh_token(token)

    refresh_token = await hass.auth.async_create_refresh_token(
        user_owner,
        client_name=CLUSTER_TOKEN_NAME,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=3650),
    )
    access_token = hass.auth.async_create_access_token(refresh_token)

    view_event = ViewEvent(
        hass,
        config,
        access_token
    )

    await view_event.get_already_registered_routes()

    return True


class ViewEvent:
    """Send route registered event to websocket."""
    registered_routes = []

    def __init__(self, hass, conf, access_token):
        self._hass = hass
        self._token = access_token
        self._components = conf[DOMAIN][CONF_COMPONENTS]
        self._name = hass.config.location_name
        self._host = hass.http.server_host
        self._port = hass.http.server_port
        hass.bus.async_listen(
            EVENT_TYPE_REQUEST_ROUTES,
            self.routes_requested_bus_handler
        )
        hass.components.websocket_api.async_register_command(
            EVENT_TYPE_REQUEST_ROUTES,
            self.routes_requested_ws_handler,
            SCHEMA_REQUEST_ROUTES
        )
        HomeAssistantView.register = self._wrap_function(
            HomeAssistantView.register
        )

    def _get_routes(self, view):
        urls = [view.url] + view.extra_urls
        routes = []

        for method in ["get", "post", "delete", "put", "patch", "head", "options"]:
            _LOGGER.debug("Checking for handler for %s", method)
            handler = getattr(view, method, None)

            if not handler:
                continue
            for url in urls:
                routes.append({
                    ATTR_ROUTE: url,
                    ATTR_METHOD: method,
                    ATTR_AUTH_REQUIRED: False,
                    ATTR_INSTANCE_NAME: self._name,
                    ATTR_INSTANCE_IP: self._host,
                    ATTR_INSTANCE_PORT: self._port,
                    ATTR_TOKEN: self._token
                })

        return routes

    def _handle_view_registration(self, view):
        routes = self._get_routes(view)
        for route in routes:
            self._handle_route_registration(route)

    def _handle_route_registration(self, route):
        url = route[ATTR_ROUTE]
        _LOGGER.debug("Checking if should register %s", url)
        _LOGGER.debug(
            "URL in components?? %s",
            str(any(component in url for component in self._components))
        )
        if not str(url).startswith('/api/services') and \
                not any(component in url for component in self._components):
            return
        self.registered_routes.append(route)
        self._fire_event(route)

    def _fire_event(self, route):
        self._hass.bus.async_fire(
            event_type=EVENT_TYPE_ROUTE_REGISTERED,
            event_data=route
        )

    def _wrap_function(self, function):
        """Wrap a function with pre and post hooks."""

        def _w(view, app, router):
            """Execute wrapped function."""
            result = function(view, app, router)

            try:
                self._handle_view_registration(view)
            except Exception as err:
                _LOGGER.error(
                    'Failed to execute post-invocation hook %s',
                    str(err)
                )

            return result

        return _w

    async def get_already_registered_routes(self):
        """Retrieve registered routes and send to websocket."""
        for route in self._hass.http.app.router.routes():
            self._handle_route_registration({
                ATTR_ROUTE: route.resource.canonical,
                ATTR_METHOD: route.method,
                ATTR_AUTH_REQUIRED: False,
                ATTR_INSTANCE_NAME: self._name,
                ATTR_INSTANCE_IP: self._host,
                ATTR_INSTANCE_PORT: self._port,
                ATTR_TOKEN: self._token
            })

    @callback
    def routes_requested_ws_handler(self, hass, connection, msg):
        """Handle websocket command requesting existing routes."""
        self._send_routes()

    @callback
    def routes_requested_bus_handler(self, event):
        """Handle event bus event for requesting existing routes."""
        if len(event.data.keys()) == 0 or event.data.get(ATTR_INSTANCE_NAME, None) == self._name:
            self._send_routes()

    def _send_routes(self):
        for route in self.registered_routes:
            self._fire_event(route)
