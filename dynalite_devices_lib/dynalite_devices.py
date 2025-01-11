"""Class to create devices from a Dynalite hub."""

import asyncio
from typing import Any, Callable, Dict, List, Optional, Set, Union

from .config import DynaliteConfig
from .const import (
    ACTIVE_ADVANCED,
    ACTIVE_INIT,
    ACTIVE_ON,
    CONF_ACT_LEVEL,
    CONF_ACTION,
    CONF_ACTION_CMD,
    CONF_ACTION_PRESET,
    CONF_ACTION_REPORT,
    CONF_ACTION_STOP,
    CONF_AREA,
    CONF_AREA_OVERRIDE,
    CONF_CHANNEL,
    CONF_CHANNEL_COVER,
    CONF_CHANNEL_TYPE,
    CONF_CLOSE_PRESET,
    CONF_DEVICE_CLASS,
    CONF_DURATION,
    CONF_FADE,
    CONF_FROM_DYNET,
    CONF_HIDDEN_ENTITY,
    CONF_LEVEL,
    CONF_NAME,
    CONF_NONE,
    CONF_OPEN_PRESET,
    CONF_PRESET,
    CONF_QUERY_CHANNEL,
    CONF_ROOM,
    CONF_ROOM_OFF,
    CONF_ROOM_ON,
    CONF_STOP_PRESET,
    CONF_TEMPLATE,
    CONF_TILT_TIME,
    CONF_TIME_COVER,
    CONF_TRGT_LEVEL,
    CONF_VALID,
    DEFAULT_CHANNEL_TYPE,
    DEFAULT_COVER_CLASS,
    EVENT_CHANNEL,
    EVENT_CONNECTED,
    EVENT_DISCONNECTED,
    EVENT_INVALIDATE,
    EVENT_PACKET,
    EVENT_PRESET,
    LOGGER,
    NOTIFICATION_PACKET,
    NOTIFICATION_PRESET,
)
from .cover import DynaliteTimeCoverDevice, DynaliteTimeCoverWithTiltDevice
from .dynalite import Dynalite
from .dynalitebase import DynaliteBaseDevice
from .event import DynetEvent
from .light import DynaliteChannelLightDevice
from .switch import (
    DynaliteChannelSwitchDevice,
    DynaliteDualPresetSwitchDevice,
    DynalitePresetSwitchDevice,
)


class DynaliteNotification:
    """A notification from the network that is sent to the application."""

    def __init__(self, notification: str, data: Dict[str, Any]):
        """Create a notification."""
        self.notification = notification
        self.data = data

    def __repr__(self):
        """Print a notification for logs."""
        return (
            "DynaliteNotification(notification="
            + self.notification
            + ", data="
            + str(self.data)
            + ")"
        )

    def __eq__(self, other):
        """Compare two notification, mostly for debug."""
        return self.notification == other.notification and self.data == other.data


class DynaliteDevices:
    """Manages a single Dynalite bridge."""

    def __init__(
        self,
        new_device_func: Callable[[List[DynaliteBaseDevice]], None],
        update_device_func: Callable[[Optional[DynaliteBaseDevice]], None],
        notification_func: Callable[[DynaliteNotification], None],
    ) -> None:
        """Initialize the system."""
        self._host = ""
        self._port = 0
        self.name = None  # public
        self._poll_timer = 0.0
        self._default_fade = 0.0
        self._default_query_channel = 0
        self._active = ""
        self._auto_discover = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._new_device_func = new_device_func
        self._update_device_func = update_device_func
        self._notification_func = notification_func
        self._configured = False
        self.connected = False  # public
        self._added_presets: Dict[int, Any] = {}
        self._added_channels: Dict[int, Any] = {}
        self._added_room_switches: Dict[int, Any] = {}
        self._added_time_covers: Dict[int, Any] = {}
        self._waiting_devices: List[DynaliteBaseDevice] = []
        self._timer_active = False
        self._timer_callbacks: Set[Callable[[], None]] = set()
        self._area: Dict[int, Any] = {}
        self._dynalite = Dynalite(broadcast_func=self.handle_event)
        self._resetting = False
        self._default_presets: Dict[int, Any] = {}

    async def async_setup(self) -> bool:
        """Set up a Dynalite bridge based on host parameter in the config."""
        LOGGER.debug("bridge async_setup")
        self._loop = asyncio.get_running_loop()
        # Run the dynalite object. Assumes self.configure() has been called
        self._resetting = False
        self.connected = await self._dynalite.connect(self._host, self._port)
        return self.connected

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure a Dynalite bridge."""
        LOGGER.debug("bridge async_configure - %s", config)
        self._configured = False
        configurator = DynaliteConfig(config)
        # insert the global values
        self._host = configurator.host
        self._port = configurator.port
        self.name = configurator.name
        self._auto_discover = configurator.auto_discover
        #BS HACK in here TODO overide mode here for testing
        self._active = configurator.active
        self._on_preset =  configurator.on_preset
        self._poll_timer = configurator.poll_timer
        self._default_fade = configurator.default_fade
        self._default_query_channel = configurator.default_query_channel
        # keep the old values in case of a reconfigure, for auto discovery
        old_area = self._area
        self._area = configurator.area
        for area in old_area:
            if area not in self._area:
                self._area[area] = old_area[area]
        self._default_presets = configurator.default_presets
        # now register the channels and presets and ask for initial status if needed
        for area in self._area:
            if self._active in [ACTIVE_INIT, ACTIVE_ON]:
                self.request_area_preset(area, self._area[area][CONF_QUERY_CHANNEL])
            for channel in self._area[area][CONF_CHANNEL]:
                self.create_channel_if_new(area, channel)
                if self._active in [ACTIVE_INIT, ACTIVE_ON]:
                    self.request_channel_level(area, channel)
            for preset in self._area[area][CONF_PRESET]:
                self.create_preset_if_new(area, preset)
        # register the rooms (switches on presets 1/4)
        # all the devices should be created for channels and presets
        self.register_rooms()
        # register the time covers
        self.register_time_covers()
        # callback for all devices
        if self._new_device_func and self._waiting_devices:
            self._new_device_func(self._waiting_devices)
            self._waiting_devices = []
        self._configured = True

    def register_rooms(self) -> None:
        """Register the room switches from two normal presets each."""
        for area, area_config in self._area.items():
            if area_config.get(CONF_TEMPLATE, "") == CONF_ROOM:
                if area in self._added_room_switches:
                    continue
                new_device = DynaliteDualPresetSwitchDevice(area, self, False)
                self._added_room_switches[area] = new_device
                new_device.set_device(
                    1, self._added_presets[area][area_config[CONF_ROOM_ON]]
                )
                new_device.set_device(
                    2, self._added_presets[area][area_config[CONF_ROOM_OFF]]
                )
                self.register_new_device(new_device)

    def register_time_covers(self) -> None:
        """Register the time covers from three presets and a channel each."""
        for area, area_config in self._area.items():
            if area_config.get(CONF_TEMPLATE, "") == CONF_TIME_COVER:
                if area in self._added_time_covers:
                    continue
                if area_config[CONF_TILT_TIME] == 0:
                    new_device = DynaliteTimeCoverDevice(
                        area, self, self._poll_timer, False
                    )
                else:
                    new_device = DynaliteTimeCoverWithTiltDevice(
                        area, self, self._poll_timer, False
                    )
                self._added_time_covers[area] = new_device
                new_device.set_device(
                    1, self._added_presets[area][area_config[CONF_OPEN_PRESET]]
                )
                new_device.set_device(
                    2, self._added_presets[area][area_config[CONF_CLOSE_PRESET]]
                )
                new_device.set_device(
                    3, self._added_presets[area][area_config[CONF_STOP_PRESET]]
                )
                if area_config[CONF_CHANNEL_COVER] != 0:
                    channel_device = self._added_channels[area][
                        area_config[CONF_CHANNEL_COVER]
                    ]
                    new_device.set_device(4, channel_device)
                self.register_new_device(new_device)

    def register_new_device(self, device: DynaliteBaseDevice) -> None:
        """Register a new device and group all the ones prior to CONFIGURED event together."""
        # after initial configuration, every new device gets sent on its own. The initial ones are bunched together
        if not device.hidden:
            if self._configured:
                self._new_device_func([device])
            else:  # send all the devices together when configured
                self._waiting_devices.append(device)

    def available(self, conf: str, area: int, item_num: Union[int, str]) -> bool:
        """Return whether a device on the bridge is available."""
        if not self.connected:
            return False
        if conf in [CONF_CHANNEL, CONF_PRESET]:
            return bool(self._area.get(area, {}).get(conf, {}).get(item_num, False))
        assert conf == CONF_TEMPLATE
        return self._area.get(area, {}).get(CONF_TEMPLATE, "") == item_num

    def update_device(self, device: Optional[DynaliteBaseDevice] = None, from_dynet = False) -> None:
        """Update one or more devices."""
        if device and device.hidden:
            return
        if not from_dynet:
            # if the update was triggered from a HA event
            self.update_DyNet_ui(device)
        self._update_device_func(device)

    def update_DyNet_ui(self, device: Optional[DynaliteBaseDevice] = None) -> None:
        """evaluate the area if every channel is 0% then let the DyNet switch know everything is off"""
        #TODO supress this behaviour if the update was triggered by a DyNet Network Source.
        if self._active == ACTIVE_ADVANCED:
            if device and device.category == 'light':
                area = device._area
                channels = device._bridge._added_channels[area]
                if len(channels) > 1: #If there is more than 1 channel in the area
                    # determine the current collective state of the area
                    area_on_state = False
                    for channel in channels:
                        channel_record = channels.get(channel, {})
                        if channel_record.is_on:
                            area_on_state = True
                            break # as soon as we find a channel on then no need to check the others
                    LOGGER.debug("----------COLLECTIVE STATE area=%s is_on=%s", area, area_on_state)
                    area_config = self._area[area]
                    # then Decide if the collective state has changes since last time?
                    if area_on_state != area_config.get('was_on',not area_on_state):
                        # if the previous state is not recorded or the state has changed then update DyNet UI
                        if area_on_state:
                            # send a report preset to the dynet newwork
                            self._dynalite.report_preset(area,self._on_preset,0)
                        else:
                            # Send a whole area channel level of 0%
                            # self._dynalite.set_channel_level(area,0,0,0)
                            # self._off_preset
                            self._dynalite.report_preset(area,4,0)
                        area_config.update({"was_on" : area_on_state}) # update previous value
                        # TODO verify that these commands will not be intepreted by the home assistant itself (only by DyNet)??

    def report_preset(self, area: int, preset: int,channel=0) -> None:
        """Report Preset to Area"""
        self._dynalite.report_preset(area,preset,channel)


    def send_notification(self, notification: DynaliteNotification) -> None:
        """Update one or more devices."""
        self._notification_func(notification)

    def handle_event(self, event: DynetEvent) -> None:
        """Handle all events."""
        LOGGER.debug("handle_event - type=%s event=%s", event.event_type, event.data)
        if event.event_type == EVENT_CONNECTED:
            LOGGER.debug("Received CONNECTED message")
            self.connected = True
            self.update_device()
        elif event.event_type == EVENT_DISCONNECTED:
            LOGGER.debug("Received DISCONNECTED message")
            self.connected = False
            self.update_device()
        elif event.event_type == EVENT_PRESET:
            LOGGER.debug("Received PRESET message")
            assert event.data
            self.handle_preset_selection(event)
            self.send_notification(
                DynaliteNotification(
                    NOTIFICATION_PRESET,
                    {
                        CONF_AREA: event.data[CONF_AREA],
                        CONF_PRESET: event.data[CONF_PRESET],
                    },
                )
            )
        elif event.event_type == EVENT_CHANNEL:
            LOGGER.debug("Received CHANNEL message")
            self.handle_channel_change(event)
        elif event.event_type == EVENT_INVALIDATE:
            LOGGER.debug("Received EVENT_INVALIDATE message")
            self.handle_preset_invalidate(event)
        else:
            assert event.event_type == EVENT_PACKET
            assert event.data
            LOGGER.debug("Received PACKET message")
            self.send_notification(
                DynaliteNotification(
                    NOTIFICATION_PACKET, {NOTIFICATION_PACKET: event.data[EVENT_PACKET]}
                )
            )


    def ensure_area(self, area: int) -> None:
        """Configure a default area if it is not yet in config."""
        if area not in self._area:
            LOGGER.debug("adding area %s that is not in config", area)
            # consider adding default presets to new areas (XXX)
            self._area[area] = DynaliteConfig.configure_area(
                area, {}, self._default_fade, self._default_query_channel, {}, {}
            )

    def create_preset_if_new(self, area: int, preset: int) -> None:
        """Register a new preset."""
        LOGGER.debug("create_preset_if_new - area=%s preset=%s", area, preset)
        # if already configured, ignore
        if self._added_presets.get(area, {}).get(preset, False):
            return
        self.ensure_area(area)
        area_config = self._area[area]
        if preset not in area_config[CONF_PRESET]:
            area_config[CONF_PRESET][preset] = DynaliteConfig.configure_preset(
                preset,
                self._default_presets.get(preset, {}),
                area_config[CONF_FADE],
                CONF_TEMPLATE in area_config or not self._auto_discover,
            )
            # if the area is a template is a template, new presets should be hidden
            if area_config.get(CONF_TEMPLATE, False):
                area_config[CONF_PRESET][preset][CONF_HIDDEN_ENTITY] = True
        hidden = area_config[CONF_PRESET][preset].get(CONF_HIDDEN_ENTITY, False)
        new_device = DynalitePresetSwitchDevice(area, preset, self, hidden)
        new_device.set_level(0)
        self.register_new_device(new_device)
        if area not in self._added_presets:
            self._added_presets[area] = {}
        self._added_presets[area][preset] = new_device
        LOGGER.debug(
            "Creating Dynalite preset area=%s preset=%s hidden=%s", area, preset, hidden
        )

    def handle_preset_selection(self, event: DynetEvent) -> None:
        """Change the selected preset."""
        assert event.data
        LOGGER.debug("handle_preset_selection - event=%s", event.data)
        area = event.data[CONF_AREA]
        preset = event.data[CONF_PRESET]
        from_dynet = event.data.get(CONF_FROM_DYNET,False)

        event_channel = event.data.get(CONF_CHANNEL, 0)
        if event_channel == 0:
            # preset to all Channels in the Area
            self.create_preset_if_new(area, preset)
            # Update all the preset devices
            for cur_preset_in_area in self._added_presets[area]:
                device = self._added_presets[area][cur_preset_in_area]
                if cur_preset_in_area == preset:
                    device.set_level(1)
                else:
                    device.set_level(0)
                self.update_device(device,from_dynet)
            # If active is set to full, query all channels in the area
            if self._active == ACTIVE_ON:
                for channel in self._area[area].get(CONF_CHANNEL, {}):
                    self.request_channel_level(area, channel)
            # If active is set to ADVANCED, Check if preset data is known if not then Query channels
            elif self._active == ACTIVE_ADVANCED:
                area_config = self._area[area]  # lookup the area object for the incoming area
                area_channels = area_config.get(CONF_CHANNEL, {})
                for channel in area_channels:
                    channel_record = area_channels.get(channel, {})
                    presets_record = channel_record.get(CONF_PRESET, {})
                    preset_record = presets_record.get(preset, {})
                    if preset_record.get(CONF_VALID,False):
                        # if stored preset is vaild then use this level
                        level = preset_record.get(CONF_LEVEL,-1)
                        if level != -1:
                            #update channel level.
                            channel_to_set = self._added_channels[area][channel]
                            channel_to_set.update_level(level, level)
                            self.update_device(channel_to_set,from_dynet)
                    else:
                        self.request_channel_level(area, channel)
        else:
            # An individual Channel in the area
            if self._active == ACTIVE_ADVANCED:
                area_config = self._area[area]  # lookup the area object for the incoming area
                area_channels = area_config.get(CONF_CHANNEL, {})
                channel_record = area_channels.get(event_channel, {})
                presets_record = channel_record.get(CONF_PRESET, {})
                preset_record = presets_record.get(preset, {})
                if preset_record.get(CONF_VALID,False):
                    # if stored preset is vaild then use this level
                    level = preset_record.get(CONF_LEVEL,-1)
                    if level != -1:
                        #update channel level.
                        channel_to_set = self._added_channels[area][event_channel]
                        channel_to_set.update_level(level, level)
                        self.update_device(channel_to_set,from_dynet)
                else:
                    self.request_channel_level(area, event_channel)

    def handle_preset_invalidate(self, event: DynetEvent) -> None:
        """Invalidate the nominated preset."""
         # if advanced mode is selected
        if self._active == ACTIVE_ADVANCED:
            LOGGER.debug("handle_preset_invalidate - event=%s", event.data)
            area = event.data[CONF_AREA]
            area_config = self._area[area]  # lookup the area object for the incoming area

            preset = 0 #Unknown Preset
            if CONF_PRESET in event.data.keys():
                preset = event.data[CONF_PRESET]
            else:
                # if the preset was not received then find the current preset for this area
                preset = self.get_current_preset(area)

            if preset == 0:
                for nPreset in area_config.get(CONF_PRESET, {}):
                    self.invalidate_preset(area, nPreset)
            else:
                # if the preset does not exist then create  a new one
                self.create_preset_if_new(area, preset)
                self.invalidate_preset(area, preset)

    def invalidate_preset(self,area: int,preset: int) -> None:
        # for each channel in the area Invalidate the stored Level
        area_config = self._area[area]
        area_channels = area_config.get(CONF_CHANNEL, {})
        for channel in area_config.get(CONF_CHANNEL, {}):   # This iterates channel numbers
            channel_record = area_channels.get(channel, {})
            if CONF_PRESET not in channel_record.keys():
                channel_record[CONF_PRESET] = {preset :{CONF_LEVEL: -1,CONF_VALID: False}}
            else:
                channel_record[CONF_PRESET] |= {preset :{CONF_LEVEL: -1,CONF_VALID: False}}
    def get_current_preset(self, area: int) -> int:
        """Find the current preset for the specified area."""
        preset = 0 #if current preset can't be found then return 0
        area_presets = self._added_presets[area]
        for aPreset in area_presets:
            preset_record = area_presets.get(aPreset, {})
            if preset_record.is_on:
                preset = aPreset
                break
        return preset

    def update_preset(self,area,channel,preset,level) -> None:
        """update the stored level for Preset"""
        area_config = self._area[area]
        if preset != 0:  #if the preset is unknown then abort operation
            # if the preset does not exist then create  a new one
            self.create_preset_if_new(area, preset)
            area_config = self._area[area]
            area_channels = area_config.get(CONF_CHANNEL, {})
            if channel == 0:
                # Update all channels
                for channel in area_config.get(CONF_CHANNEL, {}):   # This iterates channel numbers
                    channel_record = area_channels.get(channel, {})
                    if CONF_PRESET not in channel_record.keys():
                        channel_record[CONF_PRESET] = {preset :{CONF_LEVEL: level,CONF_VALID: True}}
                    else:
                        channel_record[CONF_PRESET] |= {preset :{CONF_LEVEL: level,CONF_VALID: True}}
            else:
                # Update a single channel
                channel_record = area_channels.get(channel, {})
                if CONF_PRESET not in channel_record.keys():
                    channel_record[CONF_PRESET] = {preset :{CONF_LEVEL: level,CONF_VALID: True}}
                else:
                    channel_record[CONF_PRESET] |= {preset :{CONF_LEVEL: level,CONF_VALID: True}}



    def create_channel_if_new(self, area: int, channel: int) -> None:
        """Register a new channel."""
        LOGGER.debug("create_channel_if_new - area=%s, channel=%s", area, channel)
        # if already configured, ignore
        if self._added_channels.get(area, {}).get(channel, False):
            return
        self.ensure_area(area)
        area_config = self._area[area]
        if channel not in area_config[CONF_CHANNEL]:
            area_config[CONF_CHANNEL][channel] = DynaliteConfig.configure_channel(
                channel,
                {},
                area_config[CONF_FADE],
                CONF_TEMPLATE in area_config or not self._auto_discover,
            )
        channel_config = area_config[CONF_CHANNEL][channel]
        LOGGER.debug("create_channel_if_new - channel_config=%s", channel_config)
        channel_type = channel_config.get(
            CONF_CHANNEL_TYPE, DEFAULT_CHANNEL_TYPE
        ).lower()
        hidden = channel_config.get(CONF_HIDDEN_ENTITY, False)
        if channel_type == "light":
            new_device: DynaliteBaseDevice = DynaliteChannelLightDevice(
                area, channel, self, hidden
            )
            self.register_new_device(new_device)
        elif channel_type == "switch":
            new_device = DynaliteChannelSwitchDevice(area, channel, self, hidden)
            self.register_new_device(new_device)
        else:
            LOGGER.info("unknown chnanel type %s - ignoring", channel_type)
            return
        if area not in self._added_channels:
            self._added_channels[area] = {}
        self._added_channels[area][channel] = new_device
        LOGGER.debug("Creating Dynalite channel area=%s channel=%s", area, channel)

    def handle_channel_change(self, event: DynetEvent) -> None:
        """Change the level of a channel."""
        assert event.data
        LOGGER.debug("handle_channel_change - data=%s", event.data)
        area = event.data[CONF_AREA]
        channel = event.data.get(CONF_CHANNEL, None)
        if channel:
            self.create_channel_if_new(area, channel)
        action = event.data[CONF_ACTION]
        if action == CONF_ACTION_REPORT:
            actual_level = (255 - event.data[CONF_ACT_LEVEL]) / 254
            target_level = (255 - event.data[CONF_TRGT_LEVEL]) / 254
            channel_to_set = self._added_channels[area][channel]
            channel_to_set.update_level(actual_level, target_level)
            if self._active == ACTIVE_ADVANCED:
                preset = self.get_current_preset(area)
                self.update_preset(area,channel,preset,target_level) # Update preset with target level, Make preset validated
            self.update_device(channel_to_set)
        elif action == CONF_ACTION_CMD:
            target_level = (255 - event.data[CONF_TRGT_LEVEL]) / 254
            # when there is only a "set channel level" command, assume that this is both the actual and the target
            channel_to_set = self._added_channels[area][channel]
            channel_to_set.update_level(target_level, target_level)
            self.update_device(channel_to_set)
        elif action == CONF_ACTION_STOP:
            if channel:
                channel_to_set = self._added_channels[area][channel]
                channel_to_set.stop_fade()
                self.update_device(channel_to_set)
            else:
                for channel in self._added_channels.get(area, {}):
                    channel_to_set = self._added_channels[area][channel]
                    channel_to_set.stop_fade()
                    self.update_device(channel_to_set)
        else:
            assert action == CONF_ACTION_PRESET
            assert channel  # XXX - not handling for all channels
            area_config = self._area[area]
            area_preset = area_config.get(CONF_PRESET, {})
            preset_num = event.data[CONF_PRESET]
            target_level = area_preset.get(preset_num, {}).get(CONF_LEVEL, -1)
            if target_level != -1:
                channel_to_set = self._added_channels[area][channel]
                channel_to_set.update_level(target_level, target_level)
                self.update_device(channel_to_set)

    def add_timer_listener(self, callback_func: Callable[[], None]) -> None:
        """Add a listener to the timer and start if needed."""
        self._timer_callbacks.add(callback_func)
        if not self._timer_active:
            assert self._loop
            self._loop.call_later(self._poll_timer, self.timer_func)
            self._timer_active = True

    def remove_timer_listener(self, callback_func: Callable[[], None]) -> None:
        """Remove a listener from a timer."""
        self._timer_callbacks.discard(callback_func)

    def timer_func(self) -> None:
        """Call callbacks and either schedule timer or stop."""
        if self._timer_callbacks and not self._resetting:
            assert self._loop
            cur_callbacks = self._timer_callbacks.copy()
            for callback in cur_callbacks:
                callback()
            self._loop.call_later(self._poll_timer, self.timer_func)
        else:
            self._timer_active = False

    def set_channel_level(
        self, area: int, channel: int, level: float, fade: float
    ) -> None:
        """Set the level for a channel."""
        fade = self._area[area][CONF_CHANNEL][channel][CONF_FADE]
        self._dynalite.set_channel_level(area, channel, level, fade)

    def select_preset(self, area: int, preset: int, fade: float,channel=0) -> None:
        """Select a preset in an area."""
        self._dynalite.select_preset(area, preset, fade, channel)

    def request_area_preset(self, area: int, query_channel: Optional[int]) -> None:
        """Send a request to an area to report the preset."""
        if query_channel is None:
            if area in self._area:
                query_channel = self._area[area][CONF_QUERY_CHANNEL]
            else:
                query_channel = self._default_query_channel
        self._dynalite.request_area_preset(area, query_channel)

    def request_channel_level(self, area: int, channel: int) -> None:
        """Send a request to an area to report the preset."""
        self._dynalite.request_channel_level(area, channel)

    def get_area_name(self, area: int) -> str:
        """Return the name of an area."""
        return self._area[area][CONF_NAME]

    def get_channel_name(self, area: int, channel: int) -> str:
        """Return the name of a channel."""
        cur_area = self._area.get(area, {})
        default_area_name = f"Area {area}"
        default_channel_name = f"Channel {channel}"
        return f"{cur_area.get(CONF_NAME, default_area_name)} {cur_area.get(CONF_CHANNEL, {}).get(channel, {}).get(CONF_NAME, default_channel_name)}"

    def get_channel_fade(self, area: int, channel: int) -> float:
        """Return the fade of a channel."""
        try:
            return self._area[area][CONF_CHANNEL][channel][CONF_FADE]
        except KeyError:
            return self._default_fade

    def get_preset_name(self, area: int, preset: int) -> str:
        """Return the name of a preset."""
        cur_area = self._area.get(area, {})
        area_name = cur_area.get(CONF_NAME, f"Area {area}")
        preset_name = (
            cur_area.get(CONF_PRESET, {})
            .get(preset, {})
            .get(CONF_NAME, f"Preset {preset}")
        )
        if area_name == preset_name:
            return preset_name
        return f"{area_name} {preset_name}"

    def get_preset_fade(self, area: int, preset: int) -> float:
        """Return the fade of a preset."""
        try:
            return self._area[area][CONF_PRESET][preset][CONF_FADE]
        except KeyError:
            return self._default_fade

    def get_multi_name(self, area: int) -> str:
        """Return the name of a multi-device."""
        return self._area[area][CONF_NAME]

    def get_device_class(self, area: int) -> str:
        """Return the class for a blind."""
        try:
            return self._area[area][CONF_DEVICE_CLASS]
        except KeyError:
            return DEFAULT_COVER_CLASS

    def get_cover_duration(self, area: int) -> float:
        """Return the class for a blind."""
        try:
            return self._area[area][CONF_DURATION]
        except KeyError:
            return 60

    def get_cover_tilt_duration(self, area: int) -> float:
        """Return the class for a blind."""
        try:
            return self._area[area][CONF_TILT_TIME]
        except KeyError:
            return 0

    def get_master_area(self, area: int) -> str:
        """Get the master area when combining entities from different Dynet areas to the same area."""
        assert area in self._area
        area_config = self._area[area]
        master_area = area_config[CONF_NAME]
        if CONF_AREA_OVERRIDE in area_config:
            override_area = area_config[CONF_AREA_OVERRIDE]
            master_area = override_area if override_area.lower() != CONF_NONE else ""
        return master_area

    async def async_reset(self) -> None:
        """Reset the connections and timers."""
        self._resetting = True
        await self._dynalite.async_reset()
        while self._timer_active:
            await asyncio.sleep(0.1)
