import zlib

from .CR2W_types import CR2W, LocalizedString, PROPERTY
from .anims_builder import (
    DEFAULT_BUILD_VERSION,
    DEFAULT_HEADER_VERSION,
    _add_chunk,
    _init_cr2w,
    _make_cname_prop,
    _make_enum_prop,
    _make_handle,
    _make_import_handle,
    _make_string_prop,
    _make_taglist_prop,
)


def _ptr_prop(cr2w, name, ref_idx, ptr_type):
    h = _make_handle(cr2w, ref_idx, ptr_type)
    return PROPERTY(CR2WFILE=cr2w, Handles=[h], elements=[h], theName=name, theType=ptr_type)


def _ptr_array_prop(cr2w, name, ref_indices, elem_ptr_type):
    handles = [_make_handle(cr2w, i, elem_ptr_type) for i in ref_indices]
    return PROPERTY(
        CR2WFILE=cr2w, Handles=handles, elements=handles,
        theName=name, theType=f"array:2,0,{elem_ptr_type}",
    )


def _uint32(name, value):
    return PROPERTY(Value=int(value), theName=name, theType="Uint32")


def _bool(name, value):
    return PROPERTY(Value=bool(value), theName=name, theType="Bool")


def _localized_string(name, value):
    string = LocalizedString()
    string.val = int(value)
    return PROPERTY(String=string, theName=name, theType="LocalizedString")


def _element_info(element_id, duration):
    return PROPERTY(
        theName="CStorySceneSectionVariantElementInfo",
        theType="CStorySceneSectionVariantElementInfo",
        More=[
            _make_string_prop("elementId", element_id),
            PROPERTY(Value=float(duration), theName="approvedDuration", theType="Float"),
        ],
    )


def _vector_prop(name, x, y):
    return PROPERTY(theName=name, theType="Vector", More=[
        PROPERTY(Value=float(x), theName="X", theType="Float"),
        PROPERTY(Value=float(y), theName="Y", theType="Float"),
        PROPERTY(Value=0.0, theName="Z", theType="Float"),
        PROPERTY(Value=1.0, theName="W", theType="Float"),
    ])


def _color_prop(name):
    return PROPERTY(theName=name, theType="Color", More=[
        PROPERTY(Value=0, theName="Red", theType="Uint8"),
        PROPERTY(Value=0, theName="Green", theType="Uint8"),
        PROPERTY(Value=0, theName="Blue", theType="Uint8"),
        PROPERTY(Value=255, theName="Alpha", theType="Uint8"),
    ])


def _graph_socket_props(cr2w, block_idx, name, link_idx, is_output, connection_indices):
    """Socket property list matching REDkit-saved scenes (flags 110=out, 111=in)."""
    props = [
        _ptr_prop(cr2w, "block", block_idx, "ptr:CGraphBlock"),
        _make_cname_prop("name", name),
    ]
    if connection_indices:
        props.append(_ptr_array_prop(cr2w, "connections", connection_indices, "ptr:CGraphConnection"))
    props.append(_uint32("flags", 110 if is_output else 111))
    if is_output:
        props.append(_make_enum_prop(cr2w, "placement", "ELinkedSocketPlacement", "LSP_Right"))
    props.append(_color_prop("color"))
    if is_output:
        props.append(_make_enum_prop(cr2w, "direction", "ELinkedSocketDirection", "LSD_Output"))
    props.append(_ptr_prop(cr2w, "linkElement", link_idx, "ptr:CStorySceneLinkElement"))
    return props


def _connection_props(cr2w, source_idx, destination_idx):
    return [
        _ptr_prop(cr2w, "source", source_idx, "ptr:CGraphSocket"),
        _ptr_prop(cr2w, "destination", destination_idx, "ptr:CGraphSocket"),
    ]


def build_cutscene_wrapper_scene(
    cutscene_depot_path: str,
    duration: float,
    section_name: str = "",
    ends_with_blackscreen: bool = True,
    looped: bool = False,
    point_tag: str = "",
    scene_id: int = 0,
    locale_id: int = 2,
    header_version: int = DEFAULT_HEADER_VERSION,
    build_version: int = DEFAULT_BUILD_VERSION,
    lines=None,
) -> CR2W:
    cutscene_depot_path = str(cutscene_depot_path or "").strip().replace("/", "\\")
    lines = list(lines or [])
    if not section_name:
        section_name = cutscene_depot_path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if not scene_id:
        scene_id = (zlib.crc32(cutscene_depot_path.lower().encode("utf-8")) & 0x7FFFFFFF) or 1

    cr2w = _init_cr2w(header_version, build_version)

    # Fixed chunk layout (0-based); ptr handles are written 1-based by the writer.
    SCENE, INPUT, CS, OUTPUT, VARIANT, PLAYER, SECTION, VARIANT2 = range(8)
    # Editor graph (source-file only; stripped from cooked scenes). Without it
    # REDkit shows a fresh default graph instead of the wrapper's flow.
    (GRAPH, CS_BLOCK, CS_IN, CONN_A, IN_BLOCK, IN_OUT, CONN_B, CS_OUT, CONN_C,
     OUT_BLOCK, OUT_IN, CONN_D, SEC_BLOCK, SEC_IN, SEC_OUT) = range(8, 23)
    element_id = "CutscenePlayer_2"
    line_indices = list(range(23, 23 + len(lines)))
    line_element_ids = [f"Line_{index}" for index in range(3, 3 + len(lines))]

    _add_chunk(cr2w, "CStoryScene", [
        _ptr_array_prop(cr2w, "controlParts", [INPUT, SECTION, OUTPUT, CS], "ptr:CStorySceneControlPart"),
        _ptr_array_prop(cr2w, "sections", [SECTION, CS], "ptr:CStorySceneSection"),
        _ptr_prop(cr2w, "graph", GRAPH, "ptr:CStorySceneGraph"),
        _uint32("elementIDCounter", 2 + len(lines)),
        _uint32("sectionIDCounter", 2),
        _uint32("sceneId", scene_id),
    ])

    _add_chunk(cr2w, "CStorySceneInput", [
        _ptr_prop(cr2w, "nextLinkElement", CS, "ptr:CStorySceneLinkElement"),
        PROPERTY(
            theName="voicetagMappings", theType="array:2,0,CStorySceneVoicetagMapping",
            elements=[PROPERTY(theName="CStorySceneVoicetagMapping", theType="CStorySceneVoicetagMapping", More=[])],
        ),
        _bool("isGameplay", False),
    ])

    cs_props = [
        _ptr_array_prop(cr2w, "linkedElements", [INPUT], "ptr:CStorySceneLinkElement"),
        _ptr_prop(cr2w, "nextLinkElement", OUTPUT, "ptr:CStorySceneLinkElement"),
        _uint32("nextVariantId", 1),
        _uint32("defaultVariantId", 0),
        _ptr_array_prop(cr2w, "variants", [VARIANT], "ptr:CStorySceneSectionVariant"),
        _ptr_array_prop(cr2w, "sceneElements", [PLAYER, *line_indices], "ptr:CStorySceneElement"),
        _uint32("sectionId", 2),
        _make_string_prop("sectionName", section_name),
        _bool("streamingLock", True),
    ]
    if looped:
        cs_props.append(_bool("looped", True))
    if point_tag:
        cs_props.append(_make_taglist_prop("point", [point_tag]))
    cutscene_handle = _make_import_handle(
        cr2w, "CCutsceneTemplate", cutscene_depot_path, "handle:CCutsceneTemplate"
    )
    cs_props.append(PROPERTY(
        CR2WFILE=cr2w, Handles=[cutscene_handle], elements=[cutscene_handle],
        theName="cutscene", theType="handle:CCutsceneTemplate",
    ))
    _, cs_chunk = _add_chunk(cr2w, "CStorySceneCutsceneSection", cs_props)
    # CStorySceneSection::OnSerialize appends a sceneEventElements CVariant
    # array after the property list on every (sub)section
    cs_chunk.postPropsVariantProps = []

    output_props = [_ptr_array_prop(cr2w, "linkedElements", [CS], "ptr:CStorySceneLinkElement")]
    if ends_with_blackscreen:
        output_props.append(_bool("endsWithBlackscreen", True))
    _add_chunk(cr2w, "CStorySceneOutput", output_props)

    _add_chunk(cr2w, "CStorySceneSectionVariant", [
        _uint32("id", 0),
        _uint32("localeId", locale_id),
        PROPERTY(
            theName="elementInfo", theType="array:2,0,CStorySceneSectionVariantElementInfo",
            elements=[
                _element_info(element_id, duration),
                *[
                    _element_info(line_element_id, line["approved_duration"])
                    for line_element_id, line in zip(line_element_ids, lines)
                ],
            ],
        ),
    ])

    _add_chunk(cr2w, "CStorySceneCutscenePlayer", [
        _make_string_prop("elementID", element_id),
    ])

    # Empty default section (sectionId 1), present in every shipped wrapper.
    _, section_chunk = _add_chunk(cr2w, "CStorySceneSection", [
        _uint32("nextVariantId", 1),
        _uint32("defaultVariantId", 0),
        _ptr_array_prop(cr2w, "variants", [VARIANT2], "ptr:CStorySceneSectionVariant"),
        _uint32("sectionId", 1),
    ])
    section_chunk.postPropsVariantProps = []

    _add_chunk(cr2w, "CStorySceneSectionVariant", [
        _uint32("id", 0),
        _uint32("localeId", locale_id),
    ])

    _add_chunk(cr2w, "CStorySceneGraph", [
        _ptr_array_prop(cr2w, "graphBlocks", [CS_BLOCK, OUT_BLOCK, SEC_BLOCK, IN_BLOCK], "ptr:CGraphBlock"),
    ])
    _add_chunk(cr2w, "CStorySceneCutsceneSectionBlock", [
        _ptr_array_prop(cr2w, "sockets", [CS_IN, CS_OUT], "ptr:CGraphSocket"),
        _vector_prop("position", 139.0, 50.0),
        _ptr_prop(cr2w, "section", CS, "ptr:CStorySceneSection"),
    ])
    _add_chunk(cr2w, "CStorySceneGraphSocket",
               _graph_socket_props(cr2w, CS_BLOCK, "In", CS, False, [CONN_A]))
    _add_chunk(cr2w, "CGraphConnection", _connection_props(cr2w, CS_IN, IN_OUT))
    _add_chunk(cr2w, "CStorySceneInputBlock", [
        _ptr_array_prop(cr2w, "sockets", [IN_OUT], "ptr:CGraphSocket"),
        _vector_prop("position", 50.0, 50.0),
        _ptr_prop(cr2w, "input", INPUT, "ptr:CStorySceneInput"),
    ])
    _add_chunk(cr2w, "CStorySceneGraphSocket",
               _graph_socket_props(cr2w, IN_BLOCK, "Out", INPUT, True, [CONN_B]))
    _add_chunk(cr2w, "CGraphConnection", _connection_props(cr2w, IN_OUT, CS_IN))
    _add_chunk(cr2w, "CStorySceneGraphSocket",
               _graph_socket_props(cr2w, CS_BLOCK, "Out", CS, True, [CONN_C]))
    _add_chunk(cr2w, "CGraphConnection", _connection_props(cr2w, CS_OUT, OUT_IN))
    _add_chunk(cr2w, "CStorySceneOutputBlock", [
        _ptr_array_prop(cr2w, "sockets", [OUT_IN], "ptr:CGraphSocket"),
        _vector_prop("position", 391.0, 51.0),
        _ptr_prop(cr2w, "output", OUTPUT, "ptr:CStorySceneOutput"),
    ])
    _add_chunk(cr2w, "CStorySceneGraphSocket",
               _graph_socket_props(cr2w, OUT_BLOCK, "In", OUTPUT, False, [CONN_D]))
    _add_chunk(cr2w, "CGraphConnection", _connection_props(cr2w, OUT_IN, CS_OUT))
    _add_chunk(cr2w, "CStorySceneSectionBlock", [
        _ptr_array_prop(cr2w, "sockets", [SEC_IN, SEC_OUT], "ptr:CGraphSocket"),
        _vector_prop("position", 165.0, -43.0),
        _ptr_prop(cr2w, "section", SECTION, "ptr:CStorySceneSection"),
    ])
    _add_chunk(cr2w, "CStorySceneGraphSocket",
               _graph_socket_props(cr2w, SEC_BLOCK, "In", SECTION, False, []))
    _add_chunk(cr2w, "CStorySceneGraphSocket",
               _graph_socket_props(cr2w, SEC_BLOCK, "Out", SECTION, True, []))

    for line_element_id, line in zip(line_element_ids, lines):
        line_props = [
            _make_string_prop("elementID", line_element_id),
            _make_cname_prop("voicetag", line.get("voicetag", "")),
            _make_cname_prop("speakingTo", line.get("speaking_to", "")),
            _localized_string("dialogLine", line["string_id"]),
        ]
        if line.get("voice_file_name"):
            line_props.append(_make_string_prop("voiceFileName", line["voice_file_name"]))
        if line.get("sound_event"):
            line_props.append(_make_string_prop("soundEventName", line["sound_event"], "StringAnsi"))
        # Cutscene sections require every contained line to be background VO.
        line_props.append(_bool("isBackgroundLine", True))
        _add_chunk(cr2w, "CStorySceneLine", line_props)

    # Parent IDs are 1-based; source-depot scenes use objectFlags 0.
    layout = [
        (0, 0),             # CStoryScene
        (SCENE + 1, 0),     # Input
        (SCENE + 1, 0),     # CutsceneSection
        (SCENE + 1, 0),     # Output
        (0, 0),             # Variant
        (CS + 1, 0),        # CutscenePlayer
        (SCENE + 1, 0),     # empty Section
        (0, 0),             # Variant2
        (SCENE + 1, 0),     # Graph
        (GRAPH + 1, 0),     # CutsceneSectionBlock
        (0, 0),             # socket CS in
        (0, 0),             # connection
        (GRAPH + 1, 0),     # InputBlock
        (0, 0),             # socket input out
        (0, 0),             # connection
        (0, 0),             # socket CS out
        (0, 0),             # connection
        (GRAPH + 1, 0),     # OutputBlock
        (0, 0),             # socket output in
        (0, 0),             # connection
        (GRAPH + 1, 0),     # SectionBlock
        (0, 0),             # socket section in
        (0, 0),             # socket section out
        *[(CS + 1, 0) for _line in lines],
    ]
    for idx, (parent, flags) in enumerate(layout):
        cr2w.CR2WExport[idx].parentID = parent
        cr2w.CR2WExport[idx].objectFlags = flags

    return cr2w


def save_cutscene_wrapper_scene(save_path: str, cutscene_depot_path: str, duration: float, **kwargs) -> CR2W:
    from . import cr2w_writer
    cr2w = build_cutscene_wrapper_scene(cutscene_depot_path, duration, **kwargs)
    cr2w_writer.write_w2scene(cr2w, save_path)
    return cr2w
