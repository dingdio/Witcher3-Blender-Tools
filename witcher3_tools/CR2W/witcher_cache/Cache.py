from enum import Enum
import os

class Cache:
    # Byte arrays for different cache types
    TextureIdString = [ord('H'), ord('C'), ord('X'), ord('T')]
    SoundIdString = [ord('C'), ord('S'), ord('3'), ord('W')]
    ShaderIdString = [ord('R'), ord('D'), ord('H'), ord('S')]
    CollisionIdString = [ord('C'), ord('C'), ord('3'), ord('W')]
    DepIdString = [ord('S'), ord('P'), ord('E'), ord('D')]

    class Cachetype(Enum):
        Texture = 1
        Sound = 2
        Shader = 3
        Collision = 4
        Dep = 5
        Unknown = 6

    @staticmethod
    def GetCacheTypeOfFile(path):
        try:
            with open(path, 'rb') as file:
                id_string = list(file.read(4))
                file.seek(-8, os.SEEK_END)
                id_string_back = list(file.read(4))

                if id_string_back == Cache.TextureIdString:
                    return Cache.Cachetype.Texture
                if id_string == Cache.SoundIdString:
                    return Cache.Cachetype.Sound
                if id_string == Cache.DepIdString:
                    return Cache.Cachetype.Dep
                if id_string == Cache.CollisionIdString:
                    return Cache.Cachetype.Collision
                if id_string_back == Cache.ShaderIdString:
                    return Cache.Cachetype.Shader

                return Cache.Cachetype.Unknown
        except Exception as e:
            return Cache.Cachetype.Unknown