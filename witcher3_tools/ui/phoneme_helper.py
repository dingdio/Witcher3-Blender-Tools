import logging
import bpy
import os
import string

log = logging.getLogger(__name__)

def read_phoneme_weights():
    phonemes_data = {}
    morphs_data = {}
    phoneme_list = []
    morph_list = []
    
    fileDir = os.path.dirname(os.path.realpath(__file__))
    file_path = os.path.normpath(os.path.join(fileDir, '..', 'phonemes.txt'))


    with open(file_path, 'r') as fp:
        is_header_line = True
        for line in fp:
            data = line.strip().split('\t')
            if len(data) <= 1:
                continue

            data_head = data[0]
            if is_header_line:
                header_list = data[1:]
                morph_list = header_list
                is_header_line = False
                continue

            phoneme = data_head
            weights = list(map(float, data[1:]))
            phonemes_data[phoneme] = dict(zip(header_list, weights))
            phoneme_list.append(phoneme)
            for i, morph in enumerate(header_list):
                weight = weights[i]
                if morph not in morphs_data:
                    morphs_data[morph] = {}
                morphs_data[morph][phoneme] = weight

    return phonemes_data, morphs_data, phoneme_list, morph_list

def ensure_shape_keys(obj, shape_key_names):
    if obj.data.shape_keys is None:
        obj.shape_key_add(name="Basis")
    key_blocks = obj.data.shape_keys.key_blocks
    for key_name in shape_key_names:
        if key_name not in key_blocks:
            obj.shape_key_add(name=key_name)

def _iter_driver_var_names(reserved=None):
    reserved = set(reserved or [])
    base_names = list(string.ascii_lowercase) + list(string.ascii_uppercase)
    for name in base_names:
        if name not in reserved:
            yield name
    index = 0
    while True:
        base_name = base_names[index % len(base_names)]
        suffix = index // len(base_names)
        name = f"{base_name}{suffix}"
        if name not in reserved:
            yield name
        index += 1

def _build_phoneme_terms(phoneme_weights, phoneme_list, reserved=None):
    name_iter = _iter_driver_var_names(reserved)
    terms = []
    for phoneme in phoneme_list:
        weight = phoneme_weights.get(phoneme, 0.0)
        if weight == 0.0:
            continue
        var_name = next(name_iter)
        terms.append((weight, var_name, phoneme))
    return terms

def _format_weight(weight, precision):
    text = f"{weight:.{precision}f}"
    text = text.rstrip('0').rstrip('.')
    return text if text else "0"

def _build_phoneme_expression(terms, manual_var=None, toggle_var=None, max_len=256):
    if not terms:
        return manual_var or "0.0"

    expression = ""
    precision = 6
    while precision >= 0:
        terms_str = "+".join(f"{_format_weight(weight, precision)}*{var}" for weight, var, _ in terms)
        if manual_var and toggle_var:
            expression = f"{manual_var}+({toggle_var}*({terms_str}))"
        elif manual_var:
            expression = f"{manual_var}+({terms_str})"
        else:
            expression = terms_str
        if len(expression) <= max_len:
            return expression
        precision -= 1

    return expression

def setup_phoneme_shape_key_drivers(obj, armature_obj, pose_bone_name, phoneme_list):
    shape_keys = obj.data.shape_keys
    if shape_keys is None:
        return

    key_blocks = shape_keys.key_blocks
    for phoneme in phoneme_list:
        if phoneme not in key_blocks:
            continue
        key_block = key_blocks[phoneme]
        try:
            key_block.driver_remove('value')
        except (TypeError, RuntimeError):
            pass

        fcurve = key_block.driver_add('value')
        driver = fcurve.driver
        driver.type = 'SCRIPTED'
        while driver.variables:
            driver.variables.remove(driver.variables[0])

        var = driver.variables.new()
        var.name = 'v'
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = armature_obj
        var.targets[0].data_path = f'pose.bones["{pose_bone_name}"]["{phoneme}"]'

        driver.expression = var.name

def setup_morph_shape_key_drivers(obj, armature_obj, pose_bone_name, morphs_data, phoneme_list, toggle_pose_prop=None):
    """Set up shape key drivers on morph keys.

    Each morph driver evaluates:
        shape_key[morph].value = pose_bone[morph] + (toggle * weighted_sum_of_phoneme_channels)

    ``toggle_pose_prop`` is the name of a float custom property on the pose bone that acts as
    the on/off switch (0.0 = off, 1.0 = on).  Using an OBJECT → pose-bone path here mirrors the
    pattern used for the manual ``m`` variable, which is known to work reliably.  The older
    ARMATURE → PointerProperty sub-path approach was unreliable in Blender's driver evaluation
    and caused the toggle to always evaluate as 0.

    Phoneme terms read directly from pose-bone custom properties.  This avoids a dependency chain
    (pose-bone -> phoneme shape key -> morph shape key) that can become stale in Blender until the
    driver expression is manually re-entered.
    """
    shape_keys = obj.data.shape_keys
    if shape_keys is None:
        return

    key_blocks = shape_keys.key_blocks

    for morph_name, phoneme_weights in morphs_data.items():
        if morph_name not in key_blocks:
            continue

        key_block = key_blocks[morph_name]
        try:
            key_block.driver_remove('value')
        except (TypeError, RuntimeError):
            pass

        fcurve = key_block.driver_add('value')
        driver = fcurve.driver
        driver.type = 'SCRIPTED'
        while driver.variables:
            driver.variables.remove(driver.variables[0])

        manual_var = 'm'
        manual = driver.variables.new()
        manual.name = manual_var
        manual.type = 'SINGLE_PROP'
        manual.targets[0].id_type = 'OBJECT'
        manual.targets[0].id = armature_obj
        manual.targets[0].data_path = f'pose.bones["{pose_bone_name}"]["{morph_name}"]'

        # Toggle: stored as a float custom property on the pose bone so it can be read
        # via the same reliable OBJECT → pose-bone path used by the manual variable.
        toggle_var = None
        if toggle_pose_prop:
            toggle_var = 't'
            toggle = driver.variables.new()
            toggle.name = toggle_var
            toggle.type = 'SINGLE_PROP'
            toggle.targets[0].id_type = 'OBJECT'
            toggle.targets[0].id = armature_obj
            toggle.targets[0].data_path = f'pose.bones["{pose_bone_name}"]["{toggle_pose_prop}"]'

        reserved = {manual_var}
        if toggle_var:
            reserved.add(toggle_var)

        # Phoneme terms: read from intermediate phoneme shape keys (KEY type).
        # These shape keys are driven from pose_bone[phoneme] by
        # setup_phoneme_shape_key_drivers, so values are always current.
        #
        # KEY type is intentional here — it keeps every morph driver's OBJECT
        # dependency count at exactly 2 (m + t), avoiding a cross-object cycle
        # in Blender's depsgraph that occurs during NLA playback when OBJECT-type
        # vars are used for phonemes (face mesh shape keys → main armature → face
        # rig → face mesh = cycle).  Same-datablock KEY reads are resolved in a
        # single evaluation pass with no cycle.
        terms = _build_phoneme_terms(phoneme_weights, phoneme_list, reserved=reserved)
        for weight, var_name, phoneme in terms:
            var = driver.variables.new()
            var.name = var_name
            var.type = 'SINGLE_PROP'
            var.targets[0].id_type = 'KEY'
            var.targets[0].id = shape_keys
            var.targets[0].data_path = f'key_blocks["{phoneme}"].value'

        driver.expression = _build_phoneme_expression(terms, manual_var=manual_var, toggle_var=toggle_var)

def create_phoneme_shape_keys(obj, phoneme_list):
    ensure_shape_keys(obj, phoneme_list)

def create_morphs(obj, morphs_data, phoneme_list):
    # Create mapping from variables a-z to phonemes, then A-Z
    letters = list(string.ascii_lowercase) + list(string.ascii_uppercase)
    phoneme_to_variable = {}
    variable_to_phoneme = {}
    for i, phoneme in enumerate(phoneme_list):
        if i < len(letters):
            var_name = letters[i]
            phoneme_to_variable[phoneme] = var_name
            variable_to_phoneme[var_name] = phoneme
        else:
            # Append numbers after uppercase letters are exhausted
            index = i - len(letters)
            var_name = letters[index % len(letters)] + str(index // len(letters))
            phoneme_to_variable[phoneme] = var_name
            variable_to_phoneme[var_name] = phoneme

    for morph_name, phoneme_weights in morphs_data.items():
        # Skip if morph_name is a phoneme, to avoid conflicts
        if morph_name in phoneme_list:
            continue

        # Create morph if it doesn't exist
        if morph_name not in obj.data.shape_keys.key_blocks:
            obj.shape_key_add(name=morph_name)
        morph_key = obj.data.shape_keys.key_blocks[morph_name]

        # Remove existing drivers
        morph_key.driver_remove('value')

        # Create a new driver
        driver = morph_key.driver_add('value').driver
        driver.type = 'SCRIPTED'
        expression_terms = []
        used_variables = set()
        for phoneme, weight in phoneme_weights.items():
            if weight == 0.0:
                continue
            # Map phoneme to variable
            if phoneme in phoneme_to_variable:
                var_name = phoneme_to_variable[phoneme]
            else:
                continue  # Skip phonemes without variable mapping

            # Ensure the phoneme shape key exists
            if phoneme not in obj.data.shape_keys.key_blocks:
                continue  # Skip if phoneme shape key doesn't exist

            # Add variable if not already added
            if var_name not in used_variables:
                var = driver.variables.new()
                var.name = var_name
                var.targets[0].id_type = 'KEY'
                var.targets[0].id = obj.data.shape_keys.id_data
                var.targets[0].data_path = f'key_blocks["{phoneme}"].value'
                used_variables.add(var_name)

            # Build expression term
            weight_str = f"{weight:.6f}"
            expression_terms.append(f"{weight_str}*{var_name}")

        # Set driver expression
        if expression_terms:
            driver.expression = "+".join(expression_terms)
        else:
            # If no expression terms, set value to zero
            driver.expression = "0.0"

def set_up_pose_bone_drivers(obj, armature_obj, pose_bone, morphs_data, phoneme_list):
    for morph_name in morphs_data.keys():
        if morph_name in phoneme_list:
            continue  # Skip phonemes
        # Ensure the morph exists on the object
        if morph_name not in obj.data.shape_keys.key_blocks:
            continue  # Skip if morph does not exist

        # Ensure the custom property exists on the pose bone
        if morph_name not in pose_bone:
            pose_bone[morph_name] = 0.0  # Initialize the property

        # Remove existing drivers on the property
        if armature_obj.animation_data:
            fcurves = [fcurve for fcurve in armature_obj.animation_data.drivers if fcurve.data_path == 'pose.bones["w3_face_poses"]["%s"]' % morph_name]
            for fcurve in fcurves:
                armature_obj.animation_data.drivers.remove(fcurve)
        else:
            armature_obj.animation_data_create()

        # Set up driver on the pose bone's custom property
        fcurve = pose_bone.driver_add('["%s"]' % morph_name)
        driver = fcurve.driver
        driver.type = 'SCRIPTED'
        # Remove existing variables
        while driver.variables:
            driver.variables.remove(driver.variables[0])

        # Create a new variable
        var = driver.variables.new()
        var.name = 'var'
        var.targets[0].id_type = 'KEY'
        var.targets[0].id = obj.data.shape_keys.id_data
        var.targets[0].data_path = 'key_blocks["%s"].value' % morph_name

        # Set the driver expression
        driver.expression = 'var'

def main():
    selected_obj = bpy.context.object  # Currently selected object
    if selected_obj is None:
        log.warning("Please select an object.")
        return

    if selected_obj.type == 'ARMATURE':
        armature_obj = selected_obj
        # Create a new cube mesh
        bpy.ops.mesh.primitive_cube_add()
        obj = bpy.context.active_object  # The new cube
    elif selected_obj.type == 'MESH':
        obj = selected_obj
        armature_obj = None
    else:
        log.warning("Please select a mesh or armature object.")
        return

    # Rest of the script applies to 'obj'
    phonemes_data, morphs_data, phoneme_list, morph_list = read_phoneme_weights()
    create_phoneme_shape_keys(obj, phoneme_list)
    create_morphs(obj, morphs_data, phoneme_list)

    # If an armature was selected, set up drivers from morphs to pose bone properties
    if armature_obj:
        # Ensure that the pose bone 'w3_face_poses' exists
        try:
            pose_bone = armature_obj.pose.bones['w3_face_poses']
        except KeyError:
            log.warning("Pose bone 'w3_face_poses' does not exist in the selected armature.")
            return

        # For each morph, set up drivers from the cube's morphs to the pose bone's properties
        set_up_pose_bone_drivers(obj, armature_obj, pose_bone, morphs_data, phoneme_list)

if __name__ == "__main__":
    main()
