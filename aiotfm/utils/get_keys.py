from aiotfm import __version__
from aiotfm.errors import EndpointError

import aiohttp
import json

class Keys:
	def __init__(self, **keys):
		self.auth = keys.pop('auth_key', 0)
		self.connection = keys.pop('connection_key', '')
		self.identification = keys.pop('identification_keys', [])
		self.msg = [k & 0xff for k in keys.pop('msg_keys', [])]
		self.packet = keys.pop('packet_keys', [])
		self.version = keys.pop('version', 0)
		self.server_ip = keys.pop('server_ip', '37.187.29.8')
		self.server_ports = keys.pop('server_ports', [11801, 12801, 13801, 14801])
		self.kwargs = keys

async def get_keys(client_id):
	data = {}
	payload = {"token": client_id}

	try:
		async with aiohttp.ClientSession() as session:                
			async with session.get("https://tfmkeyparser.herokuapp.com/tfm_keys", params=payload) as response:
				data = await response.json()
	except aiohttp.client_exceptions.ClientConnectorError:
		async with aiohttp.ClientSession() as session:                
			async with session.get("https://tfmkeyparser-alt.herokuapp.com/tfm_keys", params=payload) as response:
				data = await response.json()

	success = data.pop("success", False)
	error = data.pop("error", "").capitalize()

	if not success:
		raise EndpointError(error)

	keys = Keys(**data)
	if len(keys.packet) > 0 and len(keys.identification) > 0 and len(keys.msg) > 0 and keys.version != 0:
		return keys

	raise EndpointError('Something went wrong: A key is empty ! {}'.format(data))