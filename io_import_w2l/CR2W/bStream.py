import sys
import struct
import io

# This is free and unencumbered software released into the public domain.
# 
# Anyone is free to copy, modify, publish, use, compile, sell, or
# distribute this software, either in source code form or as a compiled
# binary, for any purpose, commercial or non-commercial, and by any
# means.
# 
# In jurisdictions that recognize copyright laws, the author or authors
# of this software dedicate any and all copyright interest in the
# software to the public domain. We make this dedication for the benefit
# of the public at large and to the detriment of our heirs and
# successors. We intend this dedication to be an overt act of
# relinquishment in perpetuity of all present and future rights to this
# software under copyright law.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
# 
# For more information, please refer to <http://unlicense.org>

class bStream():
	def __init__(self, data=None, path=None):
		self.isBuffered = False
		if(path is not None):
			try:
				self.fhandle = open(path, 'r+b')
			except:
				self.fhandle = open(path, 'wb')
			self.name = self.fhandle.name
		else:
			self.isBuffered = True
			self.fhandle = io.BytesIO(data)
		self.decoder = 'shift-jis'
		self.endian = '>'

	def getBuffer(self):
		return self.fhandle if not self.isBuffered else None

	def readString(self, len=0, nullTerm=False):
		if (not nullTerm):
			return self.fhandle.read(len).decode(self.decoder)   
		else:
			tString = b''
			curr = self.fhandle.read(1)
			while (curr != b'' and struct.unpack(endian+"B", curr)[0] != 0):
				try:
					tString += curr
				except Exception as e:
					print('Error "{0}" while reading string at {1}'.format(e, hex(self.fhandle.tell())))
				curr = self.fhandle.read(1)
			return tString.decode(self.decoder)

	def read(self, count): #sadly I dont get switches. It hurts.
		return self.fhandle.read(count)

	def readAll(self):
		return self.fhandle.read()

	def readUInt32(self):
		return struct.unpack(self.endian+'I', self.fhandle.read(4))[0]

	def readInt32(self):
		return struct.unpack(self.endian+'i', self.fhandle.read(4))[0]

	def readUInt16(self):
		return struct.unpack(self.endian+'H', self.fhandle.read(2))[0]

	def readInt16(self):
		return struct.unpack(self.endian+'h', self.fhandle.read(2))[0]

	def readUInt8(self):
		return struct.unpack(self.endian+'B', self.fhandle.read(1))[0]

	def readInt8(self):
		return struct.unpack(self.endian+'b', self.fhandle.read(1))[0]

	def readFloat(self):
		return struct.unpack(self.endian+'f', self.fhandle.read(4))[0]

	def readU32s(self, count):
		ret = []
		for x in range(0, count):
			ret.append(self.readUInt32())
		return ret

	def readVec3(self):
		vec = []
		vec.append(self.readFloat())
		vec.append(self.readFloat())        
		vec.append(self.readFloat())
		return vec

	def write(self, data):
		return self.fhandle.write(data)

	def writeUInt8(self, int):
		self.fhandle.write(struct.pack(self.endian+"B", int))
	
	def writeInt8(self, int):
		self.fhandle.write(struct.pack(self.endian+"b", int))

	def writeFloat(self, float):
		self.fhandle.write(struct.pack(self.endian+"f", float))

	def writeUInt16(self, int):
		self.fhandle.write(struct.pack(self.endian+"H", int))

	def writeInt16(self, int):
		self.fhandle.write(struct.pack(self.endian+"h", int))

	def writeUInt32(self, int):
		self.fhandle.write(struct.pack(self.endian+"I", int))
	
	def writeInt32(self, int):
		self.fhandle.write(struct.pack(self.endian+"i", int))

	def pad(self, count):
		for x in range(0, count):
			self.writeUInt8(0)

	def writeUInt32List(self, list):
		for x in range(0, len(list)):
			self.writeUInt32(list[x])		

	def writeUInt32s(self, int, count):
		for x in range(0, count):
			self.writeUInt32(int)

	def writeString(self, str):
		self.fhandle.write(str.encode('ASCII'))

	def seekBack(self, whence=0):
		self.fhandle.seek(self.backPos, whence)

	def seek(self, pos, whence=0):
		self.backPos = self.fhandle.tell()
		self.fhandle.seek(pos, whence)

	def padTo32(self, end):
		nextAligned = (end+(32-1)) & ~(32-1)
		delta = nextAligned - end
		self.pad(delta)

	@staticmethod
	def padTo32Delta(end):
		nextAligned = (end+(32-1)) & ~(32-1)
		return (nextAligned - end)


	def tell(self):
		return self.fhandle.tell()

	def close(self):
		self.fhandle.close()
