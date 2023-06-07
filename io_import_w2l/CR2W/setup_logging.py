import logging
my_logging_level = logging.CRITICAL
logging.basicConfig(level=my_logging_level,
                        format='%(levelname)8s %(name)s %(message)s')

for name in ('CR2W.CR2W_types', 'io_import_w2l.CR2W.CR2W_types'):
    logging.getLogger(name).setLevel(logging.CRITICAL)

for name in ('CR2W.dc_mesh', 'io_import_w2l.CR2W.dc_mesh'):
    logging.getLogger(name).setLevel(logging.CRITICAL)

def register():
    pass