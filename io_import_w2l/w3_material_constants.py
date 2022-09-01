# List of Witcher 3 shaders that will use Witcher3_Main nodegroup.
# This is only here for reference, since we default to this anyways.
NG_TO_SHADER = {
	'Witcher3_Eye_Shadow': ['pbr_eye_shadow'],
	'Witcher3_Main' : ['pbr_std',
		'pbr_std_colorshift',
		'pbr_std_tint_mask_2det',
		'pbr_std_tint_mask_2det_fresnel',
		'pbr_std_tint_mask_det',
		'pbr_std_tint_mask_det_fresnel',
		'pbr_std_tint_mask_det_pattern',
		'pbr_spec_tint_mask_det',
		'pbr_spec',
		'transparent_lit',
		'transparent_lit_vert',
		'transparent_reflective',
		'pbr_simple',
		'pbr_simple_noemissive',
		'pbr_det',
		'pbr_vert_blend',
		'snow',]
	,
	# List of Witcher 3 shaders that will use Witcher3_Skin nodegroup.
	'Witcher3_Skin' : ['pbr_skin',
		'pbr_skin_decal',
		'pbr_skin_simple',
		'pbr_skin_normalblend',
		'pbr_skin_morph']
	,
	# List of Witcher 3 shaders that will use Witcher3_Hair nodegroup.
	'Witcher3_Hair' : ['pbr_hair',
		'pbr_hair_simple',
		'pbr_hair_moving']
	,
	# List of Witcher 3 shaders that will use Witcher3_Eye nodegroup.
	'Witcher3_Eye' : ['pbr_eye']
	,
	# List of Witcher 3 shaders that will use Witcher3_Glass nodegroup.
	'Witcher3_Glass' : [
		'transparent_lit',
		'transparent_reflective'	# This should be something more like water rather than glass...
	]
	,
	# List of Witcher 3 shaders that should be invisible.
	'Invisible' : [
		'volume'
	]
}

# We want to define the shader mapping one way, but actually do the look-ups the other way.
# So let's flip the keys/values.
SHADER_MAPPING = {}
for ng_name, shaders in NG_TO_SHADER.items():
	for shader in shaders:
		SHADER_MAPPING[shader] = ng_name

PARAM_ORDER = ['Diffuse', 'Diffusemap', 'DiffuseArray',
	'Normal', 'Normalmap', 'NormalArray',
	'Ambient', 'TintMask', 'SpecularTexture', 'SpecularColor',
	'RSpecBase', 'RSpecScale',
	'Anisotropy', 'SpecularShiftTexture', 'SpecularShiftUVScale', 'SpecularShiftScale',
	'Translucency', 'TranslucencyRim', 'TranslucencyRimScale',
	'FresnelStrength', 'FresnelPower',
	'AOPower', 'AmbientPower',
	'DetailPower',
	'DetailNormal', 'DetailTile', 'DetailRange', 'DetailRotation',
	'DetailNormal1', 'DetailTile1', 'DetailRange1', 'DetailRotation1',
	'Detail1Normal', 'Detail1Tile', 'Detail1Range', 'Detail1Rotation',
	'Detail2Normal', 'Detail2Tile', 'Detail2Range', 'Detail2Rotation',
	'DetailNormal2', 'DetailTile2', 'DetailRange2', 'DetailRotation2',
	]

# TODO: I should probably go about this in a better way. It should probably be a Pin:[Equivalents] dict, not an equivalent:pin dict.
EQUIVALENT_PARAMS = {
	'Diffusemap' : 'Diffuse'
	,'Normalmap' : 'Normal'
	,'Ambientmap' : 'TintMask'

	,'DiffuseArray' : 'Diffuse'
	,'NormalArray' : 'Normal'

	,'diffuse' : 'Diffuse'
	,'normal' : 'Normal'

	,'diff' : 'Diffuse'
	,'norm' : 'Normal'
	,'Diff' : 'Diffuse'
	,'Norm' : 'Normal'

	,'Specular' : 'SpecularTexture'
	,'specular' : 'SpecularTexture'
	,'Spec' : 'SpecularTexture'
	,'spec' : 'SpecularTexture'
	}

TODO_params = ['Pattern_Array', 'Pattern_Mixer', 'Pattern_Index', 'Pattern_Offset',
	'Pattern_Size', 'Pattern_DistortionPower', 'Pattern_Rotation', 'handle:CTextureArray',
	'Pattern_Roughness_Influence', 'Pattern_Color1', 'Pattern_Color2', 'Pattern_Color3'
	]

IGNORED_PARAMS = ['DetailRange']
# IGNORED_PARAMS.extend(TODO_params)

import os
RES_FILE = "witcher3_materials.blend"
RES_DIR = os.path.dirname(os.path.realpath(__file__))
RES_PATH = os.path.join(RES_DIR, RES_FILE)