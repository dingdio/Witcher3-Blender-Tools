from enum import Enum

class Enums:
    class BlockDataObjectType:
        Invalid = 0
        Mesh = 1
        Collision = 2
        Decal = 3
        Dimmer = 4
        PointLight = 5
        SpotLight = 6
        RigidBody = 7
        Cloth = 8
        Destruction = 9
        Particles = 10

        def getEnum(num):
            arr = ["Invalid",
                    "Mesh",
                    "Collision",
                    "Decal",
                    "Dimmer",
                    "PointLight",
                    "SpotLight",
                    "RigidBody",
                    "Cloth",
                    "Destruction",
                    "Particles"]
            return arr[num]

    class EDimmerType(Enum):
        DIMMERTYPE_Default = 0
        DIMMERTYPE_InsideArea = 1
        DIMMERTYPE_OutsideArea = 2

    class ESkeletalAnimationType(Enum):
        """Docstring for ESkeletalAnimationType."""
        SAT_Normal = 0
        SAT_Additive = 1
        SAT_MS = 2
        
    class EAdditiveType(Enum):
        """Docstring for EAdditiveType."""
        AT_Local = 0
        AT_Ref = 1
        AT_TPose = 2
        AT_Animation = 3
 
    class ESkeletalAnimationTypeOTHER:
        SAT_Normal = 0
        SAT_Additive = 1
        SAT_MS = 2

        def getEnum(num):
            arr = ["SAT_Normal",
                    "SAT_Additive",
                    "SAT_MS"]
            return arr[num]

    class EJobTreeType(Enum):
        """Docstring for EJobTreeType."""
        EJTT_NothingSpecial = 0
        EJTT_Praying = 1
        EJTT_InfantInHand = 2
        EJTT_Sitting = 3
        EJT_PlayingMusic = 4
        EJTT_CatOnLap = 5
