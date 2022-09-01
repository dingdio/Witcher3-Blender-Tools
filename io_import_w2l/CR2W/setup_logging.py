import importlib
import logging

my_logging_level = logging.DEBUG
if importlib.util.find_spec("coloredlogs"):
    import coloredlogs
    coloredlogs.install(level=my_logging_level, fmt='%(name)s %(levelname)8s %(message)s')
else:
    logging.basicConfig(level=my_logging_level,
                        format='%(levelname)8s %(name)s %(message)s')

for name in ('CR2W.CR2W_types', 'io_import_w2l.CR2W.CR2W_types'):
    logging.getLogger(name).setLevel(logging.INFO)

for name in ('CR2W.dc_mesh', 'io_import_w2l.CR2W.dc_mesh'):
    logging.getLogger(name).setLevel(logging.DEBUG)

def register():
    pass