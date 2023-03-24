import logging

logging.basicConfig(level=logging.CRITICAL,
                    force=True,
                    #format='%(message)s')
                    format='%(levelname)8s %(name)s %(message)s')

for name in ('io_import_w2l',):
    logging.getLogger(name).setLevel(logging.CRITICAL)
for name in ('io_import_w2l.w3_material',
            'io_import_w2l.importers.import_blender_fun',
            'io_import_w2l.importers.import_mesh'):
    logging.getLogger(name).setLevel(logging.CRITICAL)

for name in ('io_import_w2l.importers.import_anims',):
    logging.getLogger(name).setLevel(logging.CRITICAL)

for name in ('io_import_w2l.importers.import_scene',):
    logging.getLogger(name).setLevel(logging.INFO)

for name in ('io_import_w2l.ui.ui_map'):
    logging.getLogger(name).setLevel(logging.CRITICAL)


def register():
    pass