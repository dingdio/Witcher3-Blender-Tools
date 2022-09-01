import logging

logging.basicConfig(level=logging.CRITICAL,
                    format='%(levelname)8s %(name)s %(message)s')

for name in ('blender_id', 'blender_cloud', 'io_import_w2l.import_anims'):
    logging.getLogger(name).setLevel(logging.CRITICAL)

def register():
    pass