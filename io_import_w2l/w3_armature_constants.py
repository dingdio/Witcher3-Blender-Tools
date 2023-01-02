# Dictionary to help connect the bone tails to specific bone heads
BONE_CONNECT = {
	'l_shoulder' 			: 'l_bicep'		,
	'l_bicep' 				: 'l_elbowRoll'	,
	'l_elbowRoll' 			: 'l_hand'		,
	'l_hand' 				: 'l_middle1'	,
	'l_thigh' 				: 'l_shin'		,
	'l_shin' 				: 'l_foot'		,
	'l_foot' 				: 'l_toe'		,
	'l_index_knuckleRoll' 	: 'l_index2'	,
	'l_middle_knuckleRoll' 	: 'l_middle2'	,
	'l_ring_knuckleRoll' 	: 'l_ring2'		,

	'r_shoulder' 			: 'r_bicep'		,
	'r_bicep' 				: 'r_elbowRoll'	,
	'r_elbowRoll' 			: 'r_hand'		,
	'r_hand' 				: 'r_middle1'	,
	'r_thigh' 				: 'r_shin'		,
	'r_shin' 				: 'r_foot'		,
	'r_foot' 				: 'r_toe'		,
	'r_index_knuckleRoll' 	: 'r_index2'	,
	'r_middle_knuckleRoll' 	: 'r_middle2'	,
	'r_ring_knuckleRoll' 	: 'r_ring2'		,

	'pelvis' 				: 'None'		,
	'torso' 				: 'torso2'		,
	'torso2' 				: 'torso3'		,
	'torso3' 				: 'neck'		,
	'neck' 					: 'head'		,
	'head' 					: 'None'		,
	'jaw' 					: 'chin'		,
	'tongue2' 				: 'lowwer_lip'	,
}

BONE_PARENT_DICT = {
    # spine
    'pelvis': 'torso'
    ,'torso2': 'torso'
    ,'torso3': 'torso2'
    ,'neck': 'torso3'
    ,'head': ['neck', 'neck3', 'spine3', 'spine2', 'spine1', 'torso']

    # breasts
    ,'#_boob': 'torso3'

    # legs
    ,'#_thigh': 'pelvis'
    ,'#_legRoll': 'torso'
    ,'#_legRoll2': 'torso'
    ,'#_shin': '#_thigh'
    ,'#_kneeRoll': '#_shin'
    ,'#_foot': '#_shin'
    ,'#_toe': '#_foot'

    # arms
    ,'#_shoulder': ['torso3', 'spine3']
    ,'#_shoulderRoll': '#_shoulder'
    ,'#_bicep': '#_shoulder'
    ,'#_bicep2': '#_bicep'
    ,'#_elbowRoll': '#_bicep'
    ,'#_forearmRoll1': '#_elbowRoll'
    ,'#_forearmRoll2': '#_elbowRoll'
    ,'#_handRoll': '#_elbowRoll'

    # hands
    ,'#_hand': ['#_elbowRoll', '#_forearm']
    ,'#_pinky0': '#_hand'

    ,'#_thumb1': '#_hand'
    ,'#_thumb_roll': '#_hand'
    ,'#_thumb2': '#_thumb1'
    ,'#_thumb3': '#_thumb2'

    ,'#_index_knuckleRoll': '#_hand'
    ,'#_index1': '#_hand'
    ,'#_index2': '#_index1'
    ,'#_index3': '#_index2'

    ,'#_middle_knuckleRoll': '#_hand'
    ,'#_middle1': '#_hand'
    ,'#_middle2': '#_middle1'
    ,'#_middle3': '#_middle2'

    ,'#_ring_knuckleRoll': '#_hand'
    ,'#_ring1': '#_hand'
    ,'#_ring2': '#_ring1'
    ,'#_ring3': '#_ring2'

    ,'#_pinky_knuckleRoll': '#_hand'
    ,'#_pinky1': '#_pinky0'
    ,'#_pinky2': '#_pinky1'
    ,'#_pinky3': '#_pinky2'

    # head / face
    ,'thyroid': 'head'
    ,'hroll': 'head'
    ,'jaw': 'head'
    ,'ears': 'head'
    ,'nose': 'head'
    ,'nose_base': 'head'
    ,'lowwer_lip': 'jaw'
    ,'upper_lip': 'head'
    ,'chin': 'jaw'

    ,'#_temple': 'head'
    ,'#_forehead': 'head'
    ,'#_chick1': 'head'
    ,'#_chick2': 'head'
    ,'#_chick3': 'head'
    ,'#_chick4': 'head'
    ,'#_nose1': 'head'
    ,'#_nose2': 'head'
    ,'#_nose3': 'head'
    ,'#_eyebrow1': 'head'
    ,'#_eyebrow2': 'head'
    ,'#_eyebrow3': 'head'
    ,'#_eye': 'head'

    ,'upper_#_eyelid1': 'head'
    ,'upper_#_eyelid2': 'head'
    ,'upper_#_eyelid3': 'head'
    ,'upper_#_eyelid_fold': 'head'
    ,'lowwer_#_eyelid1': 'head'
    ,'lowwer_#_eyelid2': 'head'
    ,'lowwer_#_eyelid3': 'head'
    ,'lowwer_#_eyelid_fold': 'head'

    ,'tongue_#_side' : 'tongue2'
    ,'tongue1' : 'jaw'

    ,'#_mouth_fold1': 'jaw'
    ,'#_mouth_fold2': 'head'
    ,'#_mouth_fold3': 'head'
    ,'#_mouth_fold4': 'head'
    ,'#_mouth1': 'jaw'
    ,'#_mouth2': 'jaw'
    ,'#_mouth3': 'head'
    ,'#_mouth4': 'head'
    ,'upper_#_lip': 'head'
    ,'lowwer_#_lip': 'jaw'
    ,'#_corner_lip2': 'jaw'
    ,'#_corner_lip1': 'head'

    ,'upper_#_eyelash' : 'upper_#_eyelid2'

    #util
    ,'dyng_frontbag_01': 'torso'
    ,'dyng_backbag_01' : 'pelvis'
    ,'hinge_frontrag' : 'pelvis'
    ,'dyng_back_belt_01' : 'torso2'
    ,'dyng_front_belt_01' : 'torso2'

    #succubus
    ,'dyng_tail_01': 'torso'

    # weapons
    ,'steel_sword_scabbard_3' : 'steel_sword_scabbard_2'
    ,'steel_sword_scabbard_2' : 'steel_sword_scabbard_1'
    ,'steel_sword_scabbard_1' : 'torso3'

    ,'dyng_dagger_01' : 'pelvis'

    # medallions, necklaces
    ,'dyng_pendant_01' : 'head'
    ,'dyng_necklace_01' : 'torso3'

    ,'medalion_main_01' : 'r_medalion_03'
    ,'#_medalion_03' : '#_medalion_02'
    ,'#_medalion_02' : 'torso3'

    ,'vesemir_medalion_main_01' : 'r_vesemir_medalion_02'
    ,'#_vesemir_medalion_01' : 'torso3'

    ,'dyng_#_necklace_01' : 'torso3'
    ,'dyng_m_necklace_01' : 'dyng_l_necklace_02'

    # random clothes
    ,'dyng_#_double_earing_01' : 'head'

    ,'hinge_#_collar' : 'torso3'

    # animals
    ,'#_ear' : 'head'
    ,'spine1' : 'pelvis'
    ,'tail' : 'spine1'
    ,'#_finger' : '#_hand'
    ,'#_forearm' : '#_arm'
    ,'#_arm' : '#_shoulder'
    ,'neck1' : 'spine3'
    ,'' : ''
}

human_bone_order = [
    "Root",
    "Trajectory",
    "Reference",
    "IK_r_foot",
    "IK_l_foot",
    "IK_pelvis",
    "IK_r_hand",
    "IK_l_hand",
    "IK_torso3",
    "pelvis",
    "torso",
    "torso2",
    "torso3",
    "neck",
    "head",
    "l_thigh",
    "l_shin",
    "l_foot",
    "r_thigh",
    "r_shin",
    "r_foot",
    "l_shoulder",
    "l_bicep",
    "l_forearm",
    "l_hand",
    "l_middle1",
    "l_middle2",
    "r_shoulder",
    "r_bicep",
    "r_forearm",
    "r_hand",
    "r_middle1",
    "r_middle2",
    "r_weapon",
    "l_weapon",
    "l_toe",
    "l_legRoll",
    "l_legRoll2",
    "l_kneeRoll",
    "r_toe",
    "r_legRoll",
    "r_legRoll2",
    "r_kneeRoll",
    "l_bicep2",
    "l_shoulderRoll",
    "l_elbowRoll",
    "l_forearmRoll1",
    "l_forearmRoll2",
    "l_handRoll",
    "r_bicep2",
    "r_elbowRoll",
    "r_forearmRoll1",
    "r_forearmRoll2",
    "r_handRoll",
    "r_shoulderRoll",
    "hroll",
    "l_index1",
    "l_index2",
    "l_index3",
    "l_middle3",
    "l_pinky0",
    "l_pinky1",
    "l_pinky2",
    "l_pinky3",
    "l_ring1",
    "l_ring2",
    "l_ring3",
    "l_thumb1",
    "l_thumb2",
    "l_thumb3",
    "r_index1",
    "r_index2",
    "r_index3",
    "r_middle3",
    "r_pinky0",
    "r_pinky1",
    "r_pinky2",
    "r_pinky3",
    "r_ring1",
    "r_ring2",
    "r_ring3",
    "r_thumb1",
    "r_thumb2",
    "r_thumb3",
    "l_index_knuckleRoll",
    "l_middle_knuckleRoll",
    "l_pinky_knuckleRoll",
    "l_ring_knuckleRoll",
    "l_thumb_roll",
    "r_index_knuckleRoll",
    "r_middle_knuckleRoll",
    "r_pinky_knuckleRoll",
    "r_ring_knuckleRoll",
    "r_thumb_roll"
  ]