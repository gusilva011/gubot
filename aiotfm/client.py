import asyncio
import logging
import random
import re
import sys
import traceback
import warnings
import zlib

from typing import AnyStr, Callable, List, Optional, Union, ByteString

from aiotfm.connection import Connection
from aiotfm.enums import Community, GameMode, TradeError
from aiotfm.errors import AiotfmException, AlreadyConnected, CommunityPlatformError, \
	IncorrectPassword, InvalidEvent, LoginError, ServerUnreachable, MaintenanceError
from aiotfm.friend import Friend, FriendList
from aiotfm.inventory import Inventory, InventoryItem, Trade
from aiotfm.message import Channel, ChannelMessage, Command, Message, Whisper
from aiotfm.packet import Packet
from aiotfm.player import Player, Profile
from aiotfm.room import Map, Room, RoomList
from aiotfm.shop import Shop
from aiotfm.tribe import Tribe
from aiotfm.utils import Keys, Locale, get_keys, shakikoo

logger = logging.getLogger('aiotfm')
	
def get_data(xml, tag, reversed=False):
	data = []

	tags = re.compile("\<" + tag + "(.*?)/\>")
	for elem in tags.finditer(xml):
		props = re.compile(r"(\-?\w+) ?= ?\"(\d+\.?\d*)\"")
		coords = {}
		for prop in props.finditer(elem.group(0)):
			val = int(float(prop.group(2)))
			prop_name = prop.group(1)
			if prop_name == "X":
				if reversed:
					val = 800 - val
			coords[prop_name] = val
		data.append(coords)
	return data
	
def get_map_length(xml):
	r = re.compile(r'\<C\>\<P(.*?)L="(\d+)"(.*?)/\>\<Z\>')
	length = r.match(xml)
	if length:
		return int(length.group(2))
	return 800

class Client:
	"""Represents a client that connects to Transformice.
	Two argument can be passed to the :class:`Client`.

	.. _event loop: https://docs.python.org/3/library/asyncio-eventloops.html

	Parameters
	----------
	community: Optional[:class:`int`]
		Defines the community of the client. Defaults to 0 (EN community).
	auto_restart: Optional[:class:`bool`]
		Whether the client should automatically restart on error. Defaults to False.
	loop: Optional[event loop]
		The `event loop`_ to use for asynchronous operations. If ``None`` is passed (defaults),
		the event loop used will be ``asyncio.get_event_loop()``.
	max_retries: Optional[:class:`int`]
		The maximum number of retries the client should attempt while connecting to the game.

	Attributes
	----------
	username: Optional[:class:`str`]
		The bot's username received from the server. Might be None if the bot didn't log in yet.
	room: Optional[:class:`aiotfm.room.Room`]
		The bot's room. Might be None if the bot didn't log in yet or couldn't join any room yet.
	trade: Optional[:class:`aiotfm.inventory.Trade`]
		The current trade that's going on (i.e: both traders accepted it).
	trades: :class:`list`[:class:`aiotfm.inventory.Trade`]
		All the trades that the bot participates. Most of them might be invitations only.
	inventory: Optional[:class:`aiotfm.inventory.Inventory`]
		The bot's inventory. Might be None if the bot didn't log in yet or it didn't receive
		anything.
	locale: :class:`aiotfm.locale.Locale`
		The bot's locale (translations).
	friends: Optional[:class:`aiotfm.friends.FriendList`]
		The bot's friend list
	"""
	LOG_UNHANDLED_PACKETS = False

	def __init__(
		self,
		community: Community = Community.en,
		auto_restart: bool = False,
		bot_role: bool = False,
		loop: Optional[asyncio.AbstractEventLoop] = None,
		max_retries: int = 6
	):
		self.loop: asyncio.AbstractEventLoop = loop or asyncio.get_event_loop()

		self.command_prefix: str = '!'

		self.main: Connection = Connection('main', self, self.loop)
		self.bulle: Connection = None

		self._commands: dict = {}
		self._waiters: dict = {}
		self._close_event: asyncio.Future = None
		self._sequenceId: int = 0
		self._channels: List[Channel] = []
		self._restarting: bool = False
		self._closed: bool = False
		self._logged: bool = False
		self._max_retries: int = max_retries

		self.room: Room = None
		self.trade: Trade = None
		self.trades: dict = {}
		self.inventory: Inventory = None

		self.username: str = None
		self.locale: Locale = Locale()
		self.community: Community = Community(community)

		self.friends: FriendList = None

		self.keys: Keys = None
		self.authkey: int = 0
		self.pid: int = 0

		self.auto_restart: bool = auto_restart
		self.bot_role: bool = bot_role
		self.no_bulle: bool = False

	@property
	def restarting(self) -> bool:
		return self._restarting

	@property
	def closed(self) -> bool:
		return self._closed

	def _backoff(self, n: int) -> float:
		"""Returns the numbers of seconds to wait until the n-th connection attempt. Capped at 10 minutes."""
		return random.uniform(20, 30 * 2 ** min(n, 5))

	def data_received(self, data: bytes, connection: Connection):
		"""|coro|
		Dispatches the received data.
		:param data: :class:`bytes` the received data.
		:param connection: :class:`aiotfm.Connection` the connection that received
			the data.
		"""
		# :desc: Called when a socket receives a packet. Does not interfere
		# with :meth:`Client.handle_packet`.
		# :param connection: :class:`aiotfm.Connection` the connection that received
		# the packet.
		# :param packet: :class:`aiotfm.Packet` a copy of the packet.
		self.dispatch('raw_socket', connection, Packet(data))
		self.loop.create_task(self.handle_packet(connection, Packet(data)))

	async def handle_packet(self, connection: Connection, packet: Packet) -> bool:
		"""|coro|
		Handles the known packets and dispatches events.
		Subclasses should handle only the unhandled packets from this method.

		Example: ::
			class Bot(aiotfm.Client):
				async def handle_packet(self, conn, packet):
					handled = await super().handle_packet(conn, packet.copy())

					if not handled:
						# Handle here the unhandled packets.
						pass

		:param connection: :class:`aiotfm.Connection` the connection that received
			the packet.
		:param packet: :class:`aiotfm.Packet` the packet.
		:return: True if the packet got handled, False otherwise.
		"""
		CCC = packet.readCode()
		if CCC == (1, 1): # Old packets
			oldCCC, *data = packet.readString().split(b'\x01')
			data = list(map(bytes.decode, data))
			oldCCC = tuple(oldCCC[:2])

			# :desc: Called when an old packet is received. Does not interfere
			# with :meth:`Client.handle_old_packet`.
			# :param connection: :class:`aiotfm.Connection` the connection that received
			# the packet.
			# :param oldCCC: :class:`tuple` the packet identifiers on the old protocol.
			# :param data: :class:`list` the packet data.
			self.dispatch('old_packet', connection, oldCCC, data)
			return await self.handle_old_packet(connection, oldCCC, data)
			
		elif CCC == (4, 4): # Player movement
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return

			packet.read32() # Round code

			player.moving_right, player.moving_left = packet.readBool(), packet.readBool()
			if player.moving_right and not player.moving_left:
				player.facing_right = True
			elif player.moving_left and not player.moving_right:
				player.facing_right = False

			player.x, player.y = packet.read32(), packet.read32()
			player.vx, player.vy = packet.read16(), packet.read16()

			player.jumping = packet.readBool()

			player.frame, player.on_portal = packet.read8(), packet.read8()
			
			self.dispatch('player_movement', player)
			
		elif CCC == (4, 6): # Player redirection
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return
				
			player.facing_right = packet.readBool()
			
			self.dispatch('player_direction_change', player)
		
		elif CCC == (4, 9): # Player duck
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return
				
			player.ducking = packet.readBool()
			
			self.dispatch('player_duck', player)
			
		elif CCC == (4, 10): # Player redirection
			player = self._room.get_player(pid=packet.read32())
			if player is None:
				return
				
			player.facing_right = packet.readBool()
			
			self.dispatch('player_direction_change', player)
			
		elif CCC == (5, 2):			
			self._room.map.code = packet.read32()
			packet.read16()
			self._room.round_code = packet.read8()
			packet.read16()
			
			xml = ""
			try:
				xml = zlib.decompress(packet.readBytes(packet.read16())).decode('utf-8')
			except:
				pass
				
			self._room.map.author = packet.readUTF()
			self._room.map.perm = packet.read8()

			is_reversed = packet.readBool()
			self._room.map.is_reversed = is_reversed
			
			if xml:
				self._room.map.xml = xml
				self._room.map.length = get_map_length(xml)
				self._room.map.hole_pos = get_data(xml, 'T', reversed=is_reversed)
				self._room.map.cheese_pos = get_data(xml, 'F', reversed=is_reversed)
			else:
				self._room.map = Map()
				
			self.dispatch('map_change', self._room.map)

		if CCC == (5, 21): # Joined room
			self._room = Room(official=packet.readBool(), name=packet.readUTF())

			# :desc: Called when the client has joined a room.
			# :param room: :class:`aiotfm.room.Room` the room the client has entered.
			self.dispatch('joined_room', self._room)

		elif CCC == (5, 39): # Password required for the room

			# :desc: Called when a password is required to enter a room
			# :param room: :class:`aiotfm.room.Room` the room the server is asking for a password.
			self.dispatch('room_password', Room(packet.readUTF()))

		elif CCC == (5, 51): # Christmas gift
			code = packet.read8()
			gift_id = packet.read16() # Might be an id

			# packet.read8() unknown
			# packet.read16() random number
			# packet.read16() number, -100 most of times

			self.dispatch('christmas_gift', code, gift_id)
			
		elif CCC == (6, 6): # Room message
			author = packet.readUTF()
			message = packet.readUTF()

			author = self._room.get_player(name=author, default=Player(author))
			self.dispatch('room_message', Message(author, message, self))

			if message.startswith(self.command_prefix):
				command = Command(author, message, 'room', self)
				if command.name in self._commands.keys():
					coro = self._commands[command.name]
					asyncio.ensure_future(self._run_command(coro, command.name, command, *command.args), loop=self.loop)
					
		elif CCC == (6, 9): # Room Lua message
			message = packet.readUTF()
			self.dispatch('room_lua_message', message)

		elif CCC == (6, 20): # Server message
			packet.readBool() # if False then the message will appear in the #Server channel
			t_key = packet.readUTF()
			t_args = [packet.readUTF() for i in range(packet.read8())]

			# :desc: Called when the client receives a message from the server that needs to be translated.
			# :param message: :class:`aiotfm.locale.Translation` the message translated with the
			# current locale.
			# :param *args: a list of string used as replacement inside the message.
			self.dispatch('server_message', self.locale[t_key], *t_args)

		elif CCC == (8, 1): # Play emote
			player = self._room.get_player(pid=packet.read32())
			emote = packet.read8()
			flag = packet.readUTF() if emote == 10 else ''

			# :desc: Called when a player plays an emote.
			# :param player: :class:`aiotfm.Player` the player.
			# :param emote: :class:`int` the emote's id.
			# :param flag: :class:`str` the flag's id.
			self.dispatch('emote', player, emote, flag)

		elif CCC == (8, 5): # Show emoji
			player = self._room.get_player(pid=packet.read32())
			emoji = packet.read8()

			# :desc: Called when a player is showing an emoji above its head.
			# :param player: :class:`aiotfm.Player` the player.
			# :param emoji: :class:`int` the emoji's id.
			self.dispatch('emoji', player, emoji)

		elif CCC == (8, 6): # Player won
			packet.read8()
			player = self._room.get_player(pid=packet.read32())
			player.score = packet.read16()
			order = packet.read8()
			player_time = packet.read16() / 100

			# :desc: Called when a player get the cheese to the hole.
			# :param player: :class:`aiotfm.Player` the player.
			# :param order: :class:`int` the order of the player in the hole.
			# :param player_time: :class:`float` player's time in the hole in seconds.
			self.dispatch('player_won', player, order, player_time)

		elif CCC == (8, 11): # Blue/pink Shaman
			for i, pid in enumerate([packet.read32(), packet.read32()]):
				player = self._room.get_player(pid=pid)
				if player is not None:
					player.isShaman = True
					if i == 1:
						player.isBlueShaman = True
					else:
						player.isPinkShaman = True
					self.dispatch('player_shaman_state_change', player)

		elif CCC == (8, 12): # Player Shaman state [True]
			player = self._room.get_player(pid=packet.read32())
			if player is not None:
				player.isShaman = False
				self.dispatch('player_shaman_state_change', player)

		elif CCC == (8, 16): # Profile
			# :desc: Called when the client receives the result of a /profile command.
			# :param profile: :class:`aiotfm.player.Profile` the profile.
			self.dispatch('profile', Profile(packet))
			
		elif CCC == (8, 19): # Player cheese state [True]
			player = self._room.get_player(pid=packet.read32())
			
			if player is not None:
				player.hasCheese = False	
				
				self.dispatch('player_cheese_state_change', player)

		elif CCC == (8, 20): # Shop
			# :desc: Called when the client receives the content of the shop.
			# :param shop: :class:`aiotfm.shop.Shop` the shop.
			self.dispatch('shop', Shop(packet))

		elif CCC == (8, 22): # Skills
			skills = {}
			for _ in range(packet.read8()):
				key, value = packet.read8(), packet.read8()
				skills[key] = value

			# :desc: Called when the client receives its skill tree.
			# :param skills: :class:`dict` the skills.
			self.dispatch('skills', skills)

		elif CCC == (16, 2): # Tribe invitation received
			author = packet.readUTF()
			tribe = packet.readUTF()

			# :desc: Called when the client receives an invitation to a tribe. (/inv)
			# :param author: :class:`str` the player that invited you.
			# :param tribe: :class:`str` the tribe.
			self.dispatch('tribe_inv', author, tribe)

		elif CCC == (26, 2): # Logged in successfully
			player_id = packet.read32()
			self.username = username = packet.readUTF()
			played_time = packet.read32()
			community = Community(packet.read8())
			self.pid = pid = packet.read32()

			# :desc: Called when the client successfully logged in.
			# :param uid: :class:`int` the client's unique id.
			# :param username: :class:`str` the client's username.
			# :param played_time: :class:`int` the total number of minutes the client has played.
			# :param community: :class:`aiotfm.enums.Community` the community the client has connected to.
			# :param pid: :class:`int` the client's player id.
			self.dispatch('logged', player_id, username, played_time, community, pid)

		elif CCC == (26, 3): # Handshake OK
			online_players = packet.read32()
			language = packet.readUTF()
			country = packet.readUTF()
			self.authkey = packet.read32()
			self._logged = False

			# await connection.send(Packet.new(176, 1).writeUTF(self.community.name))

			os_info = Packet.new(28, 17).writeString('en').writeString('Linux')
			os_info.writeString('LNX 29,0,0,140').write8(0)

			await connection.send(os_info)

			# :desc: Called when the client can login through the game.
			# :param online_players: :class:`int` the number of player connected to the game.
			# :param community: :class:`aiotfm.enums.Community` the community the server suggest.
			# :param country: :class:`str` the country detected from your ip.
			self.dispatch('login_ready', online_players, language, country)

		elif CCC == (26, 12): # Login result
			self._logged = False
			# :desc: Called when the client failed logging.
			# :param code: :class:`int` the error code.
			# :param code: :class:`str` error messages.
			# :param code: :class:`str` error messages.
			self.dispatch('login_result', packet.read8(), packet.readUTF(), packet.readUTF())

		elif CCC == (26, 25): # Ping
			# :desc: Called when the client receives the ping response from the server.
			self.dispatch('ping')

		elif CCC == (26, 35): # Room list
			roomlist = RoomList.from_packet(packet)
			# :desc: Dispatched when the client receives the room list
			self.dispatch('room_list', roomlist)

		elif CCC == (28, 5): # /mapcrew, /mod
			packet.read16()
			self.dispatch('staff_list', packet.readUTF())

		elif CCC == (28, 6): # Server ping
			await connection.send(Packet.new(28, 6).write8(packet.read8()))

		elif CCC == (28, 88): # Server reboot
			self.dispatch('server_reboot', packet.read32())

		elif CCC == (29, 6): # Lua logs
			# :desc: Called when the client receives lua logs from #Lua.
			# :param log: :class:`str` a log message.
			self.dispatch('lua_log', packet.readUTF())
			
		elif CCC == (29, 20): # Lua Text Area
			_id = packet.read32()
			content = packet.readUTF()
			
			callback_list = []
			if "<a href=" in content:
				a_tag = re.compile(r"\<a .*?event:(.*?)['\"]*\>")
				callback_list = [callback.group(1) for callback in a_tag.finditer(content)]
			self.dispatch('text_area', _id, content, callback_list)

		elif CCC == (31, 1): # Inventory data
			self.inventory = Inventory.from_packet(packet)
			self.inventory.client = self

			# :desc: Called when the client receives its inventory's content.
			# :param inventory: :class:`aiotfm.inventory.Inventory` the client's inventory.
			self.dispatch('inventory_update', self.inventory)

		elif CCC == (31, 2): # Update inventory item
			item_id = packet.read16()
			quantity = packet.read8()

			if item_id in self.inventory.items:
				item = self.inventory.items[item_id]
				previous = item.quantity
				item.quantity = quantity

				# :desc: Called when the quantity of an item has been updated.
				# :param item: :class:`aiotfm.inventory.InventoryItem` the new item.
				# :param previous: :class:`aiotfm.inventory.InventoryItem` the previous item.
				self.dispatch('item_update', item, previous)

			else:
				item = InventoryItem(item_id=item_id, quantity=quantity)
				self.inventory.items[item.id] = item

				# :desc: Called when the client receives a new item in its inventory.
				# :param item: :class:`aiotfm.inventory.InventoryItem` the new item.
				self.dispatch('new_item', item)

		elif CCC == (31, 5): # Trade invite
			pid = packet.read32()

			self.trades[pid] = Trade(self, self._room.get_player(pid=pid))

			# :desc: Called when received an invitation to trade.
			# :param trade: :class:`aiotfm.inventory.Trade` the trade object.
			self.dispatch('trade_invite', self.trades[pid])

		elif CCC == (31, 6): # Trade error
			name = packet.readUTF().lower()
			error = packet.read8()

			if name == self.username.lower():
				trade = self.trade
			else:
				for t in self.trades.values():
					if t.trader.lower() == name:
						trade = t
						break

			# :desc: Called when an error occurred with a trade.
			# :param trade: :class:`aiotfm.inventory.Trade` the trade that failed.
			# :param error: :class:`aiotfm.enums.TradeError` the error.
			self.dispatch('trade_error', trade, TradeError(error))
			trade._close()

		elif CCC == (31, 7): # Trade start
			pid = packet.read32()
			trade = self.trades.get(pid)

			if trade is None:
				raise AiotfmException(f'Cannot find the trade from pid {pid}.')

			trade._start()
			self.trade = trade

			# :desc: Called when a trade starts. You can access the trade object with `Client.trade`.
			self.dispatch('trade_start')

		elif CCC == (31, 8): # Trade items
			export = packet.readBool()
			id_ = packet.read16()
			quantity = (1 if packet.readBool() else -1) * packet.read8()

			items = self.trade.exports if export else self.trade.imports
			items.add(id_, quantity)

			trader = self if export else self.trade.trader
			self.trade.locked = [False, False]

			# :desc: Called when an item has been added/removed from the current trade.
			# :param trader: :class:`aiotfm.Player` the player that triggered the event.
			# :param id: :class:`int` the item's id.
			# :param quantity: :class:`int` the quantity added/removed. Can be negative.
			# :param item: :class:`aiotfm.inventory.InventoryItem` the item after the change.
			self.dispatch('trade_item_change', trader, id_, quantity, items.get(id_))

		elif CCC == (31, 9): # Trade lock
			index = packet.read8()
			locked = packet.readBool()
			if index > 1:
				self.trade.locked = [locked, locked]
				who = "both"
			else:
				self.trade.locked[index] = locked
				who = self.trade.trader if index == 0 else self

			# :desc: Called when the trade got (un)locked.
			# :param who: :class:`aiotfm.Player` the player that triggered the event.
			# :param locked: :class:`bool` either the trade got locked or unlocked.
			self.dispatch('trade_lock', who, locked)

		elif CCC == (31, 10): # Trade complete
			trade = self.trade
			self.trade._close(succeed=True)

		elif CCC == (44, 1): # Bulle switching
			if self.no_bulle:
				return

			timestamp = packet.read32()
			uid = packet.read32()
			pid = packet.read32()
			bulle_ip = packet.readUTF()			
			ports = packet.readUTF().split('-')

			if self.bulle is not None:
				self.bulle.close()

			self.bulle = Connection('bulle', self, self.loop)
			await self.bulle.connect(bulle_ip, int(random.choice(ports)))
			await self.bulle.send(Packet.new(44, 1).write32(timestamp).write32(uid).write32(pid))

		elif CCC == (44, 22): # Fingerprint offset changed
			connection.fingerprint = packet.read8()

		elif CCC == (60, 3): # Community platform
			TC = packet.read16()

			# :desc: Called when the client receives a packet from the community platform.
			# :param TC: :class:`int` the packet's code.
			# :param packet: :class:`aiotfm.Packet` the packet.
			self.dispatch('raw_cp', TC, packet.copy(copy_pos=True))

			if TC == 3: # Connected to the community platform
				await self.sendCP(28) # Request friend list

				# :desc: Called when the client is successfully connected to the community platform.
				self.dispatch('ready')

			elif TC == 32: # Friend connected
				if self.friends is None:
					return True

				friend = self.friends.get_friend(packet.readUTF())
				friend.isConnected = True

				# :desc: Called when a friend connects to the game (not entirely fetched)
				# :param friend: :class:`aiotfm.friend.Friend` friend after this update
				self.dispatch('friend_connected', friend)

			elif TC == 33: # Friend disconnected
				if self.friends is None:
					return True

				friend = self.friends.get_friend(packet.readUTF())
				friend.isConnected = False

				# :desc: Called when a friend disconnects from the game (not entirely fetched)
				# :param friend: :class:`aiotfm.friend.Friend` friend after this update
				self.dispatch('friend_disconnected', friend)

			elif TC == 34: # Friend list loaded
				self.friends = FriendList(self, packet)

				# :desc: Called when the friend list is loaded.
				# :param friends: :class:`aiotfm.friend.FriendList` the friend list
				self.dispatch('friends_loaded', self.friends)

			elif TC == 35 or TC == 36: # Friend update / addition
				if self.friends is None:
					return True

				new = Friend(self.friends, packet)
				old = self.friends.get_friend(new.name)

				if old is not None:
					if old.isSoulmate: # Not sent by the server, checked locally.
						self.friends.soulmate = new
						new.isSoulmate = True

					self.friends.friends.remove(old)
				self.friends.friends.append(new)

				if old is None:
					# :desc: Called when a friend is added
					# :param friend: :class:`aiotfm.friend.Friend` the friend
					self.dispatch('new_friend', new)

				else:
					# :desc: Called when a friend is updated
					# :param before: :class:`aiotfm.friend.Friend` friend before this update
					# :param after: :class:`aiotfm.friend.Friend` friend after this update
					self.dispatch('friend_update', old, new)
				self.dispatch('friend_room_change', new.name, new.roomName)

			elif TC == 37: # Remove friend
				if self.friends is None:
					return True

				friend = self.friends.get_friend(packet.read32())
				if friend is not None:
					if friend == self.friends.soulmate:
						self.friends.soulmate = None

					self.friends.friends.remove(friend)

					# :desc: Called when a friend is removed
					# :param friend: :class:`aiotfm.friend.Friend` the friend
					self.dispatch('friend_remove', friend)


			elif TC == 55: # Channel join result
				sequenceId = packet.read32()
				result = packet.read8()

				# :desc: Called when the client receives the result of joining a channel.
				# :param sequenceId: :class:`int` identifier returned by :meth:`Client.sendCP`.
				# :param result: :class:`int` result code.
				self.dispatch('channel_joined_result', sequenceId, result)

			elif TC == 57: # Channel leave result
				sequenceId = packet.read32()
				result = packet.read8()

				# :desc: Called when the client receives the result of leaving a channel.
				# :param sequenceId: :class:`int` identifier returned by :meth:`Client.sendCP`.
				# :param result: :class:`int` result code.
				self.dispatch('channel_left_result', sequenceId, result)

			elif TC == 59: # Channel /who result
				idSequence = packet.read32()
				result = packet.read8()
				players = [Player(packet.readUTF()) for _ in range(packet.read16())]

				# :desc: Called when the client receives the result of the /who command in a channel.
				# :param idSequence: :class:`int` the reference to the packet that performed the request.
				# :param players: List[:class:`aiotfm.Player`] the list of players inside the channel.
				self.dispatch('channel_who', idSequence, players)

			elif TC == 62: # Joined a channel
				name = packet.readUTF()

				if name in self._channels:
					channel = [c for c in self._channels if c == name][0]
				else:
					channel = Channel(name, self)
					self._channels.append(channel)

				# :desc: Called when the client joined a channel.
				# :param channel: :class:`aiotfm.message.Channel` the channel.
				self.dispatch('channel_joined', channel)

			elif TC == 63: # Quit a channel
				name = packet.readUTF()
				if name in self._channels:
					self._channels.remove(name)

				# :desc: Called when the client leaves a channel.
				# :param name: :class:`str` the channel's name.
				self.dispatch('channel_closed', name)

			elif TC == 64: # Channel message
				username, community = packet.readUTF(), packet.read32()
				channel_name, message = packet.readUTF(), packet.readUTF()
				channel = self.get_channel(channel_name)

				author = self._room.get_player(username=username)
				if author is None:
					author = Player(username)

				if channel is None:
					channel = Channel(channel_name, self)
					self._channels.append(channel)

				channel_message = ChannelMessage(author, community, message, channel)

				# :desc: Called when the client receives a message from a channel.
				# :param message: :class:`aiotfm.message.ChannelMessage` the message.
				self.dispatch('channel_message', channel_message)

			elif TC == 65: # Tribe message
				author, message = Player(packet.readUTF()), packet.readUTF()
				author = self._room.get_player(name=author, default=author)

				# :desc: Called when the client receives a message from the tribe.
				# :param author: :class:`str` the message's author.
				# :param message: :class:`str` the message's content.
				self.dispatch('tribe_message', author, message)
				
				if message.startswith(self.command_prefix):
					command = Command(author, message, 'tribe', self)
					if command.name in self._commands.keys():
						coro = self._commands[command.name]
						asyncio.ensure_future(self._run_command(coro, command.name, command, *command.args), loop=self.loop)

			elif TC == 66: # Whisper
				author = Player(packet.readUTF())
				commu = packet.read32()
				receiver = Player(packet.readUTF())
				message = packet.readUTF()

				if self._room is not None:
					author = self._room.get_player(name=author, default=author)
					receiver = self._room.get_player(name=receiver, default=receiver)

				# :desc: Called when the client receives a whisper.
				# :param message: :class:`aiotfm.message.Whisper` the message.
				self.dispatch('whisper', Whisper(author, commu, receiver, message, self))
				
				if message.startswith(self.command_prefix):
					command = Command(author, message, 'whisper', self)
					if command.name in self._commands.keys():
						coro = self._commands[command.name]
						asyncio.ensure_future(self._run_command(coro, command.name, command, *command.args), loop=self.loop)

			elif TC == 86: # Tribe recruitment
				self.dispatch('tribe_recruitment', packet.readUTF(), packet.readUTF())

			elif TC == 88: # tribe member connected
				# :desc: Called when a tribe member connected.
				# :param name: :class:`str` the member's name.
				self.dispatch('member_connected', packet.readUTF())

			elif TC == 90: # tribe member disconnected
				# :desc: Called when a tribe member disconnected.
				# :param name: :class:`str` the member's name.
				self.dispatch('member_disconnected', packet.readUTF())

			else:
				if self.LOG_UNHANDLED_PACKETS:
					print(CCC, TC, bytes(packet.buffer)[4:])
				return False

		elif CCC == (144, 1): # Set player list
			before = self._room.players
			self._room.players = {}

			for _ in range(packet.read16()):
				player = Player.from_packet(packet)
				self._room.players[player.pid] = player

			# :desc: Called when the client receives an update of all player in the room.
			# :param before: Dict[:class:`aiotfm.Player`] the list of player before the update.
			# :param players: Dict[:class:`aiotfm.Player`] the list of player updated.
			self.dispatch('bulk_player_update', before, self._room.players)

		elif CCC == (144, 2): # Add a player
			after = Player.from_packet(packet)
			before = self._room.players.pop(after.pid, None)

			self._room.players[after.pid] = after
			if before is None:
				# :desc: Called when a player joined the room.
				# :param player: :class:`aiotfm.Player` the player.
				self.dispatch('player_join', after)
			else:
				# :desc: Called when a player's data on the room has been updated.
				# :param before: :class:`aiotfm.Player` the player before the update.
				# :param player: :class:`aiotfm.Player` the player updated.
				self.dispatch('player_update', before, after)
				
		elif CCC == (144, 6): # Player cheese state [False]
			player = self._room.get_player(pid=packet.read32())
			
			if player is not None:
				player.hasCheese = packet.readBool()
				
				self.dispatch('player_cheese_state_change', player)

		elif CCC == (144, 7): # Player Shaman state [False]
			player = self._room.get_player(pid=packet.read32())
			if player is not None:
				player.isShaman = False
				self.dispatch('player_shaman_state_change', player)

		else:
			if self.LOG_UNHANDLED_PACKETS:
				print(CCC, bytes(packet.buffer)[2:])
			return False

		return True

	async def handle_old_packet(self, connection: Connection, oldCCC: tuple, data: list) -> bool:
		"""|coro|
		Handles the known packets from the old protocol and dispatches events.
		Subclasses should handle only the unhandled packets from this method.

		Example: ::
			class Bot(aiotfm.Client):
				async def handle_old_packet(self, conn, oldCCC, data):
					handled = await super().handle_old_packet(conn, data.copy())

					if not handled:
						# Handle here the unhandled packets.
						pass

		:param connection: :class:`aiotfm.Connection` the connection that received
			the packet.
		:param oldCCC: :class:`tuple` the packet identifiers on the old protocol.
		:param data: :class:`list` the packet data.
		:return: True if the packet got handled, False otherwise.
		"""
		if oldCCC == (8, 5): # Player died
			player = self._room.get_player(pid=data[0])
			if player is not None:
				player.score = int(data[2])

				# :desc: Called when a player dies.
				# :param player: :class:`aiotfm.Player` the player.
				self.dispatch('player_died', player)

		elif oldCCC == (8, 7): # Remove a player
			player = self._room.players.pop(int(data[0]), None)
			if player is not None:
				# :desc: Called when a player leaves the room.
				# :param player: :class:`aiotfm.Player` the player.
				self.dispatch('player_remove', player)

		elif oldCCC == (8, 21): # New synchronizer (pid)
			self.dispatch('synchronizer_change', int(data[0]))

		else:
			if self.LOG_UNHANDLED_PACKETS:
				print("[OLD]", oldCCC, data)
			return False

		return True

	def get_channel(self, name: str) -> Optional[Channel]:
		"""Returns a channel from it's name or None if not found.
		:param name: :class:`str` the name of the channel.
		:return: :class:`aiotfm.message.ChannelMessage` or None
		"""
		if name is None:
			return None

		for channel in self._channels:
			if channel.name == name:
				return channel

	def get_trade(self, player: Union[str, Player]) -> Optional[Trade]:
		"""Returns the pending/current trade with a player.
		:param player: :class:`aiotfm.Player` or :class:`str` the player.
		:return: :class:`aiotfm.inventory.Trade` the trade with the player.
		"""
		if not isinstance(player, (str, Player)):
			raise TypeError(f"Expected Player or str types got {type(player)}")

		if isinstance(player, Player):
			return self.trades.get(player.pid)

		player = player.lower()
		for trade in self.trades.values():
			if trade.trader.lower() == player:
				return trade
				
	def command(self, coro: Callable) -> Callable:
		name = coro.__name__

		if not asyncio.iscoroutinefunction(coro):
			message = "Couldn't register a non-coroutine function for the command {}.".format(name)
			raise InvalidEvent(message)

		self._commands[name] = coro

		return coro

	def event(self, coro: Callable) -> Callable:
		"""A decorator that registers an event.

		More about events [here](Events.md).
		"""
		name = coro.__name__
		if not name.startswith('on_'):
			raise InvalidEvent("'{}' isn't a correct event naming.".format(name))
		if not asyncio.iscoroutinefunction(coro):
			message = "Couldn't register a non-coroutine function for the event {}.".format(name)
			raise InvalidEvent(message)

		setattr(self, name, coro)
		return coro

	def wait_for(
		self,
		event: str,
		condition: Optional[Callable] = None,
		timeout: Optional[float] = None,
		stopPropagation: bool = False
	) -> asyncio.Future:
		"""Wait for an event.

		Example: ::
			@client.event
			async def on_room_message(author, message):
				if message == 'id':
					await client.sendCommand('profile '+author)
					profile = await client.wait_for('on_profile', lambda p: p.username == author)
					await client.sendRoomMessage('Your id: {}'.format(profile.id))

		:param event: :class:`str` the event name.
		:param condition: Optionnal[`function`] A predicate to check what to wait for.
			The arguments must meet the parameters of the event being waited for.
		:param timeout: Optionnal[:class:`float`] the number of seconds before
			throwing asyncio.TimeoutError
		:return: [`asyncio.Future`](https://docs.python.org/3/library/asyncio-future.html#asyncio.Future)
			a future that you must await.
		"""
		event = event.lower()
		future = self.loop.create_future()

		if condition is None:
			def everything(*a):
				return True
			condition = everything

		if event not in self._waiters:
			self._waiters[event] = []

		self._waiters[event].append((condition, future, stopPropagation))

		return asyncio.wait_for(future, timeout)

	async def _run_event(self, coro: Callable, event_name: str, *args, **kwargs) -> bool:
		"""|coro|
		Runs an event and handle the error if any.

		:param coro: a coroutine function.
		:param event_name: :class:`str` the event's name.
		:param args: arguments to pass to the coro.
		:param kwargs: keyword arguments to pass to the coro.

		:return: :class:`bool` whether the event ran successfully or not
		"""
		try:
			await coro(*args, **kwargs)
			return True
		# except asyncio.CancelledError:
		# 	raise
		except Exception as e:
			if hasattr(self, 'on_error'):
				try:
					await self.on_error(event_name, e, *args, **kwargs)
				# except asyncio.CancelledError:
				# 	raise
				except Exception:
					if self.auto_restart:
						await self.restart(5)
					else:
						self.close()

		return False
		
	async def _run_command(self, coro: Callable, command_name: str, command: Command, *args) -> bool:
		try:
			await coro(command, *args)
			return True
		except Exception as e:
			if hasattr(self, 'on_error'):
				await self.on_error(command_name, e, *args)
		return False

	def dispatch(self, event: str, *args, **kwargs):
		"""Dispatches events

		:param event: :class:`str` event's name. (without 'on_')
		:param args: arguments to pass to the coro.
		:param kwargs: keyword arguments to pass to the coro.

		:return: [`Task`](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task)
			the _run_event wrapper task
		"""
		method = 'on_' + event

		if method in self._waiters:
			to_remove = []
			waiters = self._waiters[method]
			for i, (cond, fut, stop) in enumerate(waiters):
				if fut.cancelled():
					to_remove.append(i)
					continue

				try:
					result = bool(cond(*args))
				except Exception as e:
					fut.set_exception(e)
				else:
					if result:
						fut.set_result(args[0] if len(args) == 1 else args if len(args) > 0 else None)
						if stop:
							del waiters[i]
							return None
						to_remove.append(i)

			if len(to_remove) == len(waiters):
				del self._waiters[method]
			else:
				for i in to_remove[::-1]:
					del waiters[i]

		coro = getattr(self, method, None)
		if coro is not None:
			dispatch = self._run_event(coro, method, *args, **kwargs)
			return asyncio.ensure_future(dispatch, loop=self.loop)

	async def on_error(self, event: str, err: Exception, *a, **kw):
		"""Default on_error event handler. Prints the traceback of the error."""
		logger.error('An error occurred while dispatching the event "%s":', event, exc_info=-3)

	async def on_connection_error(self, conn: Connection, error: Exception):
		"""Default on_connection_error event handler. Prints the error."""
		logger.error('The %s connection has been closed.', conn.name, exc_info=error)

	async def on_login_result(self, code: int, *args):
		"""Default on_login_result handler. Raise an error and closes the connection."""
		self.loop.call_later(3, self.close)
		if code == 1:
			raise AlreadyConnected()
		if code == 2:
			raise IncorrectPassword()
		raise LoginError(code)

	async def _connect(self):
		"""|coro|
		Creates a connection with the main server.
		"""

		if self._close_event is None:
			raise AiotfmException(f'{self._connect.__name__} should not be called directly. Use start() instead.')

		for port in random.sample(self.keys.server_ports, 4):
			try:
				await self.main.connect(self.keys.server_ip, port)
			except Exception:
				logger.debug(f'Unable to connect to the server "{self.keys.server_ip}:{port}".')
			else:
				break
		else:
			raise ServerUnreachable('Unable to connect to the server.')

		while not self.main.open:
			await asyncio.sleep(0)

	async def sendHandshake(self):
		"""|coro|
		Sends the handshake packet so the server recognizes this socket as a player.
		"""
		packet = Packet.new(28, 1).write16(self.keys.version).writeString('en').writeString(self.keys.connection)
		packet.writeString('Desktop').writeString('-').write32(0x1fbd).writeString('')
		packet.writeString('74696720697320676f6e6e61206b696c6c206d7920626f742e20736f20736164')
		packet.writeString(
			"A=t&SA=t&SV=t&EV=t&MP3=t&AE=t&VE=t&ACC=t&PR=t&SP=f&SB=f&DEB=f&V=LNX 32,0,0,182&M=Adobe"
			" Linux&R=1920x1080&COL=color&AR=1.0&OS=Linux&ARCH=x86&L=en&IME=t&PR32=t&PR64=t&LS=en-U"
			"S&PT=Desktop&AVD=f&LFD=f&WD=f&TLS=t&ML=5.1&DP=72")
		packet.write32(0).write32(0x6257).writeString('')

		await self.main.send(packet)

	async def start(self, client_id: Optional[str] = None, **kwargs):
		"""|coro|
		Starts the client.
		"""
		self.keys = await get_keys(client_id)

		if 'username' in kwargs and 'password' in kwargs:
			# Monkey patch the on_login_ready event
			if hasattr(self, 'on_login_ready'):
				event = getattr(self, 'on_login_ready')
				self.on_login_ready = lambda *a: asyncio.gather(self.login(**kwargs), event(*a))
			else:
				self.on_login_ready = lambda *a: self.login(**kwargs)

		retries = 0
		on_started = None
		keep_alive = Packet.new(26, 26)
		while True:
			self._close_event = asyncio.Future()

			try:
				logger.info('Connecting to the game.')
				await self._connect()
				await self.sendHandshake()
				await self.locale.load()
				retries = 0 # Connection successful
				self._restarting = False
			except Exception as e:
				logger.error('Connection to the server failed.', exc_info=e)
				if on_started is not None:
					on_started.set_exception(e)
				elif retries > self._max_retries:
					raise e
				else:
					retries += 1
					backoff = self._backoff(retries)
					logger.info('Attempt %d failed. Reconnecting in %.2fs', retries, backoff)
					await asyncio.sleep(backoff)
					continue
			else:
				if on_started is not None:
					on_started.set_result(None)

			while not self._close_event.done():
				# Keep the connection alive
				await asyncio.gather(*[c.send(keep_alive) for c in (self.main, self.bulle) if c])
				await asyncio.wait((self._close_event,), timeout=15)

			reason, delay, on_started = self._close_event.result()
			self._close_event = asyncio.Future()

			logger.debug('[Close Event] Reason: %s, Delay: %d, Callback: %s', reason, delay, on_started)
			logger.debug('Will restart: %s', reason != 'stop' and self.auto_restart)

			# clean up
			for conn in (self.main, self.bulle):
				if conn is not None:
					conn.close()

			if reason == 'stop' or not self.auto_restart:
				break

			await asyncio.sleep(delay)

			# If we don't recreate the connection, we won't be able to connect.
			self.main = Connection('main', self, self.loop)
			self.bulle = None

			# Fetch some fresh keys
			if not self.bot_role and (reason != 'restart' or self.keys is None):
				for i in range(self._max_retries):
					try:
						self.keys = await get_keys(client_id)
						break
					except MaintenanceError:
						if i == 0:
							logger.info('The game is under maintenance.')

						await asyncio.sleep(30)
				else:
					raise MaintenanceError('The game is under heavy maintenance.')

	async def restart_soon(self, delay: float = 5.0, **kwargs):
		"""|coro|
		Restarts the client in several seconds.
		:param delay: :class:`float` the delay before restarting. Default is 5 seconds.
		:param args: arguments to pass to the :meth:`Client.restart` method.
		:param kwargs: keyword arguments to pass to the :meth:`Client.restart` method."""
		warnings.warn('`Client.restart_soon` is deprecated, use `Client.restart` instead.', DeprecationWarning)
		await self.restart(delay, **kwargs)

	async def restart(self, delay: float = 0, keys: Optional[Keys] = None):
		"""Restarts the client.

		:param keys:"""

		# :desc: Notify when the client restarts.
		if not self.auto_restart or self._close_event is None:
			raise AiotfmException(
				'Unable to restart the Client. Either `auto_restart` is set to '
				'False or you have not started the Client using `Client.start`.'
			)

		if self._restarting or self._close_event.done():
			return

		self.keys = keys
		self._restarting = True
		# :desc: Notify when the client restarts.
		self.dispatch("restart")

		restarted = asyncio.Future()
		self._close_event.set_result(('restart', delay, restarted))
		await restarted

	async def login(self, username: str, password: str, encrypted: bool = True, room: str = '1'):
		"""|coro|
		Log in the game.

		:param username: :class:`str` the client username.
		:param password: :class:`str` the client password.
		:param encrypted: Optional[:class:`bool`] whether the password is already encrypted or not.
		:param room: Optional[:class:`str`] the room where the client will be logged in.
		"""
		if self._logged:
			raise AiotfmException('You cannot log in twice.')

		self._logged = True
		if not encrypted:
			password = shakikoo(password)

		packet = Packet.new(26, 8).writeString(username).writeString(password)
		packet.writeString("app:/TransformiceAIR.swf/[[DYNAMIC]]/2/[[DYNAMIC]]/4").writeString(room)

		if self.bot_role:
			packet.write8(0).writeString('')
		else:
			packet.write32(self.authkey ^ self.keys.auth)
			packet.write8(0).writeString('')
			packet.cipher(self.keys.identification)

		await self.main.send(Packet.new(176, 1).writeUTF(self.community.name))
		await self.main.send(packet.write8(0))

	def run(self, username: str, password: str, **kwargs):
		"""A blocking call that do the event loop initialization for you.

		Equivalent to: ::
			@bot.event
			async def on_login_ready(*a):
				await bot.login(username, password)

			loop = asyncio.get_event_loop()
			loop.create_task(bot._start(api_id, api_token))
			loop.run_forever()
		"""
		try:
			self.loop.run_until_complete(self.start(username=username, password=password, **kwargs))
		finally:
			self.loop.run_until_complete(self.loop.shutdown_asyncgens())
			self.loop.close()

	def close(self):
		"""Closes the sockets."""
		if self._closed:
			return

		self._closed = True
		self._close_event.set_result(('stop', 0, None))

	async def sendCP(self, code: int, data: Union[Packet, ByteString] = b'') -> int:
		"""|coro|
		Send a packet to the community platform.

		:param code: :class:`int` the community platform code.
		:param data: :class:`aiotfm.Packet` or :class:`bytes` the data.
		:return: :class:`int` returns the sequence id.
		"""
		self._sequenceId = sid = (self._sequenceId + 1) % 0XFFFFFFFF

		packet = Packet.new(60, 3).write16(code)
		packet.write32(self._sequenceId).writeBytes(data)
		await self.main.send(packet, cipher=True)

		return sid

	async def sendRoomMessage(self, message: str):
		"""|coro|
		Send a message to the room.

		:param message: :class:`str` the content of the message.
		"""
		packet = Packet.new(6, 6).writeString(message)

		await self.bulle.send(packet, cipher=True)

	async def sendTribeMessage(self, message: str):
		"""|coro|
		Send a message to the tribe.

		:param message: :class:`str` the content of the message.
		"""
		await self.sendCP(50, Packet().writeString(message))

	async def sendChannelMessage(self, channel: Union[Channel, str], message: str):
		"""|coro|
		Send a message to a public channel.

		:param channel: :class:`str` the channel's name.
		:param message: :class:`str` the content of the message.
		"""
		if isinstance(channel, Channel):
			channel = channel.name

		return await self.sendCP(48, Packet().writeString(channel).writeString(message))

	async def whisper(self, username: Union[Player, str], message: AnyStr, overflow: bool = False):
		"""|coro|
		Whisper to a player.

		:param username: :class:`str` the player to whisper.
		:param message: :class:`str` the content of the whisper.
		:param overflow: :class:`bool` will send the complete message if True, splitted
			in several messages.
		"""
		if isinstance(username, Player):
			username = username.username

		async def send(msg):
			await self.sendCP(52, Packet().writeString(username).writeString(msg))

		if isinstance(message, str):
			message = message.encode()
		message = message.replace(b'<', b'&lt;').replace(b'>', b'&gt;')

		await send(message[:255])
		for i in range(255, len(message), 255):
			await asyncio.sleep(1)
			await self.whisper(username, message[i:i + 255])

	async def silenceWhisper(self, state=3, message=""):
		await self.sendCP(60, Packet().write8(state).writeString(message))

	async def getTribe(self, disconnected: bool = True) -> Optional[Tribe]:
		"""|coro|
		Gets the client's :class:`aiotfm.Tribe` and return it

		:param disconnected: :class:`bool` if True retrieves also the disconnected members.
		:return: :class:`aiotfm.Tribe` or ``None``.
		"""
		sid = await self.sendCP(108, Packet().writeBool(disconnected))

		def is_tribe(tc, packet):
			return (tc == 109 and packet.read32() == sid) or tc == 130

		tc, packet = await self.wait_for('on_raw_cp', is_tribe, timeout=5)
		if tc == 109:
			result = packet.read8()
			if result == 1:
				tc, packet = await self.wait_for('on_raw_cp', lambda tc, p: tc == 130, timeout=5)
			elif result == 17:
				return None
			else:
				raise CommunityPlatformError(118, result)
		return Tribe(packet)

	async def getRoomList(self, gamemode: Union[GameMode, int] = 0, timeout: float = 3) -> Optional[RoomList]:
		"""|coro|
		Get the room list

		:param gamemode: Optional[:class:`aiotfm.enums.GameMode`] the room's gamemode.
		:param timeout: Optional[:class:`int`] timeout in seconds. Defaults to 3 seconds.
		:return: :class:`aiotfm.room.RoomList` the room list for the given gamemode or None
		"""
		await self.main.send(Packet.new(26, 35).write8(int(gamemode)))

		def predicate(roomlist):
			return gamemode == 0 or roomlist.gamemode == gamemode

		try:
			return await self.wait_for('on_room_list', predicate, timeout=timeout)
		except asyncio.TimeoutError:
			return None
		
	async def addFriend(self, player):
		await self.sendCP(18, Packet().writeString(player))

	async def playEmote(self, emote: int, flag: str = 'fr'):
		"""|coro|
		Play an emote.

		:param emote: :class:`int` the emote's id.
		:param flag: Optional[:class:`str`] the flag for the emote id 10. Defaults to 'fr'.
		"""
		packet = Packet.new(8, 1).write8(emote).write32(0)
		if emote == 10:
			packet.writeString(flag)

		await self.bulle.send(packet)

	async def sendSmiley(self, smiley: int):
		"""|coro|
		Makes the client showing a smiley above it's head.

		:param smiley: :class:`int` the smiley's id. (from 0 to 9)
		"""
		if smiley < 0 or smiley > 9:
			raise AiotfmException('Invalid smiley id')

		packet = Packet.new(8, 5).write8(smiley)

		await self.bulle.send(packet)

	async def loadLua(self, lua_code: AnyStr):
		"""|coro|
		Load a lua code in the room.

		:param lua_code: :class:`str` or :class:`bytes` the lua code to send.
		"""
		if isinstance(lua_code, str):
			lua_code = lua_code.encode()

		await self.bulle.send(Packet.new(29, 1).write24(len(lua_code)).writeBytes(lua_code))

	async def sendCommand(self, command: str):
		"""|coro|
		Send a command to the game.

		:param command: :class:`str` the command to send.
		"""
		await self.main.send(Packet.new(6, 26).writeString(command[:255]), cipher=True)

	async def enterTribe(self):
		"""|coro|
		Enter the tribe house
		"""
		await self.main.send(Packet.new(16, 1))

	async def enterTribeHouse(self):
		"""|coro|
		Alias for :meth:`Client.enterTribe`
		"""
		await self.enterTribe()

	async def joinRoom(
		self,
		room_name: str,
		password: Optional[str] = None,
		community: Optional[int] = None,
		auto: bool = False
	):
		"""|coro|
		Join a room.
		The event 'on_joined_room' is dispatched when the client has successfully joined the room.

		:param password: :class:`str` if given the client will ignore `community` and `auto` parameters
			and will connect to the room with the given password.
		:param room_name: :class:`str` the room's name.
		:param community: Optional[:class:`int`] the room's community.
		:param auto: Optional[:class:`bool`] joins a random room (I think).
		"""
		if password is not None:
			packet = Packet.new(5, 39).writeString(password).writeString(room_name)
		else:
			packet = Packet.new(5, 38).writeString(Community(community or self.community).name)
			packet.writeString(room_name).writeBool(auto)

		await self.main.send(packet)

	async def joinChannel(self, name: str, permanent: bool = True):
		"""|coro|
		Join a #channel.
		The event 'on_channel_joined' is dispatched when the client has successfully joined
		a channel.

		:param name: :class:`str` the channel's name
		:param permanent: Optional[:class:`bool`] if True (default) the server will automatically
		reconnect the user to this channel when logged in.
		"""
		await self.sendCP(54, Packet().writeString(name).writeBool(permanent))

	async def leaveChannel(self, channel: Union[Channel, str]):
		"""|coro|
		Leaves a #channel.

		:param channel: :class:`aiotfm.message.Channel` channel to leave.
		"""
		if isinstance(channel, Channel):
			name = channel.name
		else:
			name = channel

		await self.sendCP(56, Packet().writeString(name))

	async def enterInvTribeHouse(self, author: str):
		"""|coro|
		Join the tribe house of another player after receiving an /inv.

		:param author: :class:`str` the author's username who sent the invitation.
		"""
		await self.main.send(Packet.new(16, 2).writeString(author))

	async def recruit(self, username: Union[Player, str]):
		"""|coro|
		Send a recruit request to a player.

		:param player: :class:`str` the player's username you want to recruit.
		"""
		if isinstance(username, Player):
			username = username.username
		await self.sendCP(78, Packet().writeString(username))

	async def requestShopList(self):
		"""|coro|
		Send a request to the server to get the shop list."""
		await self.main.send(Packet.new(8, 20))

	async def startTrade(self, player: Union[Player, str]):
		"""|coro|
		Starts a trade with the given player.

		:param player: :class:`aiotfm.Player` the player to trade with.
		:return: :class:`aiotfm.inventory.Trade` the resulting trade"""
		if isinstance(player, Player) and player.pid == -1:
			player = player.username

		if isinstance(player, str):
			player = self._room.get_player(username=player)
			if player is None:
				raise AiotfmException("The player must be in your room to start a trade.")

		trade = Trade(self, player)

		self.trades[player.pid] = trade
		await trade.accept()
		return trade

	async def requestInventory(self):
		"""|coro|
		Send a request to the server to get the bot's inventory."""
		await self.main.send(Packet.new(31, 1))
		
	async def getProfile(self, username: str, timeout:int = 3) -> Optional[Profile]: 
		username = username.lower()

		def check(p):
			if '#' not in username:
				return p.username.split('#')[0].lower() == username
			return p.username.lower() == username

		try:
			await self.sendCommand('profile ' + username)
			return await self.wait_for('on_profile', check, timeout=timeout)
		except asyncio.TimeoutError:
			return None
