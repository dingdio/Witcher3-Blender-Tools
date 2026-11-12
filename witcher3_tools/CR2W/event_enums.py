"""W3 event enum values from engine headers and playerTypes.ws; no local RTTI dependency."""

EVENT_ENUMS = {
    'EApertureValue': [(f'APERTURE_{n}', i) for i, n in enumerate(
        ('1_0', '1_4', '2_0', '2_8', '4_0', '5_6', '8_0', '11_0', '16_0', '22_0', '32_0'))],  # cameraDirector.h
    'EAnimEffectAction': [('EA_Start', 0), ('EA_Stop', 1)],  # animEffectEvent.h
    'EDropAction': [('DA_DropRightHand', 0), ('DA_DropLeftHand', 1), ('DA_DropAny', 2)],  # extAnimItemEvents.h
    'EFarPlaneDistance': [('FP_CrazyClose20m', 0), ('FP_EvenCloser30m', 1), ('FP_VeryClose40m', 2), ('FP_Close150m', 3), ('FP_Medium600m', 4), ('FP_DefaultEnv', 5), ('FP_Far1000m', 6), ('FP_MoreFar1500m', 7), ('FP_VeryFar2000m', 8), ('FP_CustomValue', 9)],  # cameraComponent.h
    'EGettingItem': [('GI_ByName', 0), ('GI_ByCategory', 1)],  # extAnimItemEvents.h
    'EItemAction': [('IA_Mount', 0), ('IA_MountToHand', 1), ('IA_MountToLeftHand', 2), ('IA_MountToRightHand', 3), ('IA_MountToCustomSlot1', 4), ('IA_MountToCustomSlot2', 5), ('IA_Unmount', 6)],  # extAnimItemEvents.h
    'EItemEffectAction': [('IEA_Start', 0), ('IEA_Stop', 1)],  # extAnimItemEvents.h
    'EItemLatentAction': [('ILA_Draw', 0), ('ILA_Holster', 1), ('ILA_Switch', 2)],  # itemEntity.h
    'ELookAtLevel': [('LL_Body', 0), ('LL_Head', 1), ('LL_Eyes', 2), ('LL_Null', 3)],  # actorInterface.h
    'ENearPlaneDistance': [('NP_VeryClose5cm', 0), ('NP_Close10cm', 1), ('NP_DefaultEnv', 2), ('NP_Medium25cm', 3), ('NP_Further40cm', 4), ('NP_Far60cm', 5), ('NP_VeryFar120cm', 6), ('NP_Extreme240cm', 7), ('NP_CustomValue', 8)],  # cameraComponent.h
    'EProjectileCastPosition': [('PCP_Bone', 0), ('PCP_EntityRoot', 1)],  # extAnimProjectileEvent.h
    'ERotationRate': [('RR_0', 0), ('RR_5', 5), ('RR_30', 30), ('RR_60', 60), ('RR_90', 90), ('RR_180', 180), ('RR_360', 360), ('RR_1080', 1080), ('RR_2160', 2160)],  # playerTypes.ws
    'ESide': [('S_Unknown', -1), ('S_Left', 0), ('S_Right', 1)],  # enumSide.h
}
