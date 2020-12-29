from aiotfm import __version__
from aiotfm.errors import EndpointError, InternalError, MaintenanceError

from base64 import b64encode

import aiohttp
import json

class Keys:
	"""Represents the keys used by the client to communicate to the server."""
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
	
	try:
		payload = {"id": client_id}

		async with aiohttp.ClientSession() as session:                
			async with session.post("http://renandev.tk/tfm_keys", data=payload) as response:
				if response.status == 200:
					result = await response.text()
					if "ERR_" not in result:
						data = json.loads(result)
	except:
		print("[Endpoint] Error while trying to get the keys")
		
	if data:
		keys = Keys(**data)

		if len(keys.packet) > 0 and len(keys.identification) > 0 and len(keys.msg) > 0:
			return keys

		raise Exception("Something goes wrong: A key is empty. {}".format(data))
	else:
		raise Exception("Can't get the keys.")