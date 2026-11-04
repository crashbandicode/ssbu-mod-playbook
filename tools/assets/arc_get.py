"""Extract named files from the local stock data.arc. Usage: arc_get.py <out-dir> <arc path>..."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'zshim'))
sys.path.insert(0, '/home/intpa/ssbu-recomp/tools/nro')
import arc_extract  # noqa: E402

out_dir = sys.argv[1]
arc = arc_extract.Arc('/home/intpa/ssbu-recomp/build-linux/dump/romfs/data.arc')
for name in sys.argv[2:]:
    data = arc.get(name)
    dest = os.path.join(out_dir, 'stock-' + os.path.basename(name))
    with open(dest, 'wb') as f:
        f.write(data)
    print(name, len(data), '->', dest)
