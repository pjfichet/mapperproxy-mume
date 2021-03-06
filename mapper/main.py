# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.


import os
import socket
try:
	import certifi
	import ssl
except ImportError:
	ssl = None
from telnetlib import IAC, GA, DONT, DO, WONT, WILL, theNULL, SB, SE, TTYPE, NAWS
import threading

from .mapper import USER_DATA, MUD_DATA, Mapper
from .mpi import MPI
from .utils import getDirectoryPath, touch, unescapeXML


LISTENING_STATUS_FILE = os.path.join(getDirectoryPath("."), "mapper_ready.ignore")
CHARSET = chr(42).encode("us-ascii")
SB_REQUEST, SB_ACCEPTED, SB_REJECTED, SB_TTABLE_IS, SB_TTABLE_REJECTED, SB_TTABLE_ACK, SB_TTABLE_NAK = (chr(i).encode("us-ascii") for i in range(1, 8))


class Proxy(threading.Thread):
	def __init__(self, client, server, mapper):
		threading.Thread.__init__(self)
		self.name = "Proxy"
		self._client = client
		self._server = server
		self._mapper = mapper
		self.alive = threading.Event()

	def close(self):
		self.alive.clear()

	def run(self):
		userCommands = [func[len("user_command_"):].encode("us-ascii", "ignore") for func in dir(self._mapper) if func.startswith("user_command_")]
		self.alive.set()
		while self.alive.isSet():
			try:
				data = self._client.recv(4096)
			except socket.timeout:
				continue
			except EnvironmentError:
				self.close()
				continue
			if not data:
				self.close()
			elif data.strip() and data.strip().split()[0] in userCommands:
				self._mapper.queue.put((USER_DATA, data))
			else:
				try:
					self._server.sendall(data)
				except EnvironmentError:
					self.close()
					continue


class Server(threading.Thread):
	def __init__(self, client, server, mapper, outputFormat, interface, promptTerminator):
		threading.Thread.__init__(self)
		self.name = "Server"
		self._client = client
		self._server = server
		self._mapper = mapper
		self._outputFormat = outputFormat
		self._interface = interface
		self._promptTerminator = promptTerminator
		self.alive = threading.Event()

	def close(self):
		self.alive.clear()

	def run(self):
		self.alive.set()
		normalFormat = self._outputFormat == "normal"  # NOQA: F841
		tinTinFormat = self._outputFormat == "tintin"
		rawFormat = self._outputFormat == "raw"
		ignoreBytes = frozenset([ord(theNULL), 0x11])
		negotiationBytes = frozenset(ord(byte) for byte in [DONT, DO, WONT, WILL])
		ordIAC = ord(IAC)
		ordGA = ord(GA)
		ordSB = ord(SB)
		ordSE = ord(SE)
		ordLF = ord("\n")
		ordCHARSET = ord(CHARSET)
		charsetSep = b";"
		charsets = {
			"ascii": b"US-ASCII",
			"latin-1": b"ISO-8859-1",
			"utf-8": b"UTF-8"
		}
		defaultCharset = charsets["ascii"]
		inIAC = False
		inSubOption = False
		inCharset = False
		inCharsetResponse = False
		inMPI = False
		mpiThreads = []
		mpiCounter = 0
		mpiCommand = None
		mpiLen = None
		mpiBuffer = bytearray()
		clientBuffer = bytearray()
		tagBuffer = bytearray()
		textBuffer = bytearray()
		lineBuffer = bytearray()
		charsetResponseBuffer = bytearray()
		charsetResponseCode = None
		readingTag = False
		inGratuitous = False
		modeNone = 0
		modeRoom = 1
		modeName = 2
		modeDescription = 3
		modeExits = 4
		modePrompt = 5
		modeTerrain = 6
		xmlMode = modeNone
		tagReplacements = {
			b"prompt": b"PROMPT:",
			b"/prompt": b":PROMPT",
			b"name": b"NAME:",
			b"/name": b":NAME",
			b"tell": b"TELL:",
			b"/tell": b":TELL",
			b"narrate": b"NARRATE:",
			b"/narrate": b":NARRATE",
			b"pray": b"PRAY:",
			b"/pray": b":PRAY",
			b"say": b"SAY:",
			b"/say": b":SAY",
			b"emote": b"EMOTE:",
			b"/emote": b":EMOTE"
		}
		initialOutput = b"".join((IAC, DO, TTYPE, IAC, DO, NAWS))
		encounteredInitialOutput = False
		while self.alive.isSet():
			try:
				data = self._server.recv(4096)
			except EnvironmentError:
				self.close()
				continue
			if not data:
				self.close()
				continue
			elif not encounteredInitialOutput and data.startswith(initialOutput):
				# The connection to Mume has been established, and the game has just responded with the login screen.
				# Identify for Mume Remote Editing.
				self._server.sendall(b"~$#EI\n")
				# Turn on XML mode.
				self._server.sendall(b"~$#EX2\n3G\n")
				# Tell the Mume server to put IAC-GA at end of prompts.
				self._server.sendall(b"~$#EP2\nG\n")
				# Tell the server that we will negotiate the character set.
				self._server.sendall(IAC + WILL + CHARSET)
				inCharset = True
				encounteredInitialOutput = True
			for byte in bytearray(data):
				if inIAC:
					clientBuffer.append(byte)
					if byte in negotiationBytes:
						# This is the second byte in a 3-byte telnet option sequence.
						# Skip the byte, and move on to the next.
						continue
					# From this point on, byte is the final byte in a 2-3 byte telnet option sequence.
					inIAC = False
					if byte == ordSB:
						# Sub-option negotiation begin
						inSubOption = True
					elif byte == ordSE:
						# Sub-option negotiation end
						if inCharset and inCharsetResponse:
							# IAC SE was erroneously added to the client buffer. Remove it.
							del clientBuffer[-2:]
							charsetResponseCode = None
							del charsetResponseBuffer[:]
							inCharsetResponse = False
							inCharset = False
						inSubOption = False
					elif inSubOption:
						# Ignore subsequent bytes until the sub option negotiation has ended.
						continue
					elif byte == ordIAC:
						# This is an escaped IAC byte to be added to the buffer.
						mpiCounter = 0
						if inMPI:
							mpiBuffer.append(byte)
							# IAC + IAC was appended to the client buffer earlier.
							# It must be removed as MPI data should not be sent to the mud client.
							del clientBuffer[-2:]
						elif xmlMode == modeNone:
							lineBuffer.append(byte)
					elif byte == ordCHARSET and inCharset and clientBuffer[-3:] == IAC + DO + CHARSET:
						# Negotiate the character set.
						self._server.sendall(IAC + SB + CHARSET + SB_REQUEST + charsetSep + defaultCharset + IAC + SE)
						# IAC + DO + CHARSET was appended to the client buffer earlier.
						# It must be removed as character set negotiation data should not be sent to the mud client.
						del clientBuffer[-3:]
					elif byte == ordGA:
						# Replace the IAC-GA sequence (used by the game to terminate a prompt) with the user specified prompt terminator.
						del clientBuffer[-2:]
						clientBuffer.extend(self._promptTerminator)
						self._mapper.queue.put((MUD_DATA, ("iac_ga", b"")))
						if xmlMode == modeNone:
							lineBuffer.extend(b"\r\n")
				elif byte == ordIAC:
					clientBuffer.append(byte)
					inIAC = True
				elif inSubOption or byte in ignoreBytes:
					if byte == ordCHARSET and inCharset and clientBuffer[-2:] == IAC + SB:
						# Character set negotiation responses should *not* be sent to the client.
						del clientBuffer[-2:]
						inCharsetResponse = True
					elif inCharsetResponse and byte not in ignoreBytes:
						if charsetResponseCode is None:
							charsetResponseCode = byte
						else:
							charsetResponseBuffer.append(byte)
					else:
						clientBuffer.append(byte)
				elif inMPI:
					if byte == ordLF and mpiCommand is None and mpiLen is None:
						# The first line of MPI data was recieved.
						# The first byte is the MPI command, E for edit, V for view.
						# The remaining byte sequence is the length of the MPI data to be received.
						if mpiBuffer[0:1] in (b"E", b"V") and mpiBuffer[1:].isdigit():
							mpiCommand = mpiBuffer[0:1]
							mpiLen = int(mpiBuffer[1:])
						else:
							# Invalid MPI command or length.
							inMPI = False
						del mpiBuffer[:]
					else:
						mpiBuffer.append(byte)
						if mpiLen is not None and len(mpiBuffer) >= mpiLen:
							# The last byte in the MPI data has been reached.
							mpiThreads.append(MPI(client=self._client, server=self._server, isTinTin=tinTinFormat, command=mpiCommand, data=bytes(mpiBuffer)))
							mpiThreads[-1].start()
							del mpiBuffer[:]
							mpiCommand = None
							mpiLen = None
							inMPI = False
				elif byte == 126 and mpiCounter == 0 and clientBuffer.endswith(b"\n") or byte == 36 and mpiCounter == 1 or byte == 35 and mpiCounter == 2:
					# Byte is one of the first 3 bytes in the 4-byte MPI sequence (~$#E).
					mpiCounter += 1
				elif byte == 69 and mpiCounter == 3:
					# Byte is the final byte in the 4-byte MPI sequence (~$#E).
					inMPI = True
					mpiCounter = 0
				elif readingTag:
					mpiCounter = 0
					if byte == 62:  # >
						# End of XML tag reached.
						if xmlMode == modeNone:
							if tagBuffer.startswith(b"exits"):
								xmlMode = modeExits
							elif tagBuffer.startswith(b"prompt"):
								xmlMode = modePrompt
							elif tagBuffer.startswith(b"room"):
								xmlMode = modeRoom
							elif tagBuffer.startswith(b"movement"):
								self._mapper.queue.put((MUD_DATA, ("movement", bytes(tagBuffer)[8:].replace(b" dir=", b"", 1).split(b"/", 1)[0])))
						elif xmlMode == modeRoom:
							if tagBuffer.startswith(b"name"):
								xmlMode = modeName
							elif tagBuffer.startswith(b"description"):
								xmlMode = modeDescription
							elif tagBuffer.startswith(b"terrain"):
								# Terrain tag only comes up in blindness or fog
								xmlMode = modeTerrain
							elif tagBuffer.startswith(b"gratuitous"):
								inGratuitous = True
							elif tagBuffer.startswith(b"/gratuitous"):
								inGratuitous = False
							elif tagBuffer.startswith(b"/room"):
								self._mapper.queue.put((MUD_DATA, ("dynamic", bytes(textBuffer))))
								xmlMode = modeNone
						elif xmlMode == modeName and tagBuffer.startswith(b"/name"):
							self._mapper.queue.put((MUD_DATA, ("name", bytes(textBuffer))))
							xmlMode = modeRoom
						elif xmlMode == modeDescription and tagBuffer.startswith(b"/description"):
							self._mapper.queue.put((MUD_DATA, ("description", bytes(textBuffer))))
							xmlMode = modeRoom
						elif xmlMode == modeTerrain and tagBuffer.startswith(b"/terrain"):
							xmlMode = modeRoom
						elif xmlMode == modeExits and tagBuffer.startswith(b"/exits"):
							self._mapper.queue.put((MUD_DATA, ("exits", bytes(textBuffer))))
							xmlMode = modeNone
						elif xmlMode == modePrompt and tagBuffer.startswith(b"/prompt"):
							self._mapper.queue.put((MUD_DATA, ("prompt", bytes(textBuffer))))
							xmlMode = modeNone
						if tinTinFormat:
							clientBuffer.extend(tagReplacements.get(bytes(tagBuffer), b""))
						del tagBuffer[:]
						del textBuffer[:]
						readingTag = False
					else:
						tagBuffer.append(byte)
					if rawFormat:
						clientBuffer.append(byte)
				elif byte == 60:  # <
					# Start of new XML tag.
					mpiCounter = 0
					readingTag = True
					if rawFormat:
						clientBuffer.append(byte)
				else:
					# Byte is not part of a Telnet negotiation, MPI negotiation, or XML tag name.
					mpiCounter = 0
					if xmlMode == modeNone:
						if byte == ordLF and lineBuffer:
							for line in bytes(lineBuffer).splitlines():
								if line.strip():
									self._mapper.queue.put((MUD_DATA, ("line", line)))
							del lineBuffer[:]
						else:
							lineBuffer.append(byte)
					else:
						textBuffer.append(byte)
					if rawFormat or not inGratuitous:
						clientBuffer.append(byte)
			data = bytes(clientBuffer)
			try:
				self._client.sendall(data if rawFormat else unescapeXML(data, isbytes=True))
			except EnvironmentError:
				self.close()
				continue
			del clientBuffer[:]
		if self._interface != "text":
			# Shutdown the gui
			with self._mapper._gui_queue_lock:
				self._mapper._gui_queue.put(None)
		# Join the MPI threads (if any) before joining the Mapper thread.
		for mpiThread in mpiThreads:
			mpiThread.join()


def main(outputFormat, interface, promptTerminator, gagPrompts, findFormat, localHost, localPort, remoteHost, remotePort, noSsl):
	outputFormat = outputFormat.strip().lower()
	interface = interface.strip().lower()
	if not promptTerminator:
		promptTerminator = IAC + GA
	if not gagPrompts:
		gagPrompts = False
	if interface != "text":
		try:
			import pyglet
		except ImportError:
			print("Unable to find pyglet. Disabling the GUI")
			interface = "text"
	proxySocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	proxySocket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
	proxySocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	proxySocket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
	proxySocket.bind((localHost, localPort))
	proxySocket.listen(1)
	touch(LISTENING_STATUS_FILE)
	clientConnection, proxyAddress = proxySocket.accept()
	clientConnection.settimeout(1.0)
	serverConnection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	serverConnection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
	serverConnection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
	if not noSsl and ssl is not None:
		serverConnection = ssl.wrap_socket(serverConnection, cert_reqs=ssl.CERT_REQUIRED, ca_certs=certifi.where(), ssl_version=ssl.PROTOCOL_TLS)
	try:
		serverConnection.connect((remoteHost, remotePort))

	except TimeoutError:
		try:
			clientConnection.sendall(b"\r\nError: server connection timed out!\r\n")
			clientConnection.sendall(b"\r\n")
			clientConnection.shutdown(socket.SHUT_RDWR)
		except EnvironmentError:
			pass
		clientConnection.close()
		try:
			os.remove(LISTENING_STATUS_FILE)
		except:  # NOQA: E722
			pass
		return
	if not noSsl and ssl is not None:
		# Validating server identity with ssl module
		# See https://wiki.python.org/moin/SSL
		for field in serverConnection.getpeercert()["subject"]:
			if field[0][0] == "commonName":
				certhost = field[0][1]
				if certhost != "mume.org":
					raise ssl.SSLError("Host name 'mume.org' doesn't match certificate host '{}'".format(certhost))
	mapperThread = Mapper(client=clientConnection, server=serverConnection, outputFormat=outputFormat, interface=interface, promptTerminator=promptTerminator, gagPrompts=gagPrompts, findFormat=findFormat)
	proxyThread = Proxy(client=clientConnection, server=serverConnection, mapper=mapperThread)
	serverThread = Server(client=clientConnection, server=serverConnection, mapper=mapperThread, outputFormat=outputFormat, interface=interface, promptTerminator=promptTerminator)
	serverThread.start()
	proxyThread.start()
	mapperThread.start()
	if interface != "text":
		pyglet.app.run()
	serverThread.join()
	try:
		serverConnection.shutdown(socket.SHUT_RDWR)
	except EnvironmentError:
		pass
	mapperThread.queue.put((None, None))
	mapperThread.join()
	try:
		clientConnection.sendall(b"\r\n")
		proxyThread.close()
		clientConnection.shutdown(socket.SHUT_RDWR)
	except EnvironmentError:
		pass
	proxyThread.join()
	serverConnection.close()
	clientConnection.close()
	try:
		os.remove(LISTENING_STATUS_FILE)
	except:  # NOQA: E722
		pass
