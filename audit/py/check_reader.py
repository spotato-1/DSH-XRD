# Fast check: can AutoFP's pyfullprof reader load a given pcr?
# (Runs in seconds instead of a full fp2k cycle.)
#
# WHY THIS MATTERS: pyfullprof's pcrfilereader only understands AutoFP's own
# template layout; it dies at line 749 on the pcr the official CIF converter
# writes. This script is the cheap way to prove that on ANY new pcr before
# blaming the converter — feed it the project's own reference pcr too and it
# fails there as well.
#
# run:  python audit/py/check_reader.py <pcr> [<pcr> ...]
# env:  DSH_AUTOFP_DIR = the AutoFP tree supplying pyfullprof (no default: AutoFP
#        source is not redistributed, so this machine must be told where it is)
import os
import sys
import traceback

AUTOFP = os.environ.get('DSH_AUTOFP_DIR')
if not AUTOFP or not os.path.isdir(AUTOFP):
    raise SystemExit('set DSH_AUTOFP_DIR to the AutoFP tree that provides pyfullprof '
                     '(the source is not shipped with this package)')
sys.path.insert(0, AUTOFP)
os.chdir(AUTOFP)
from pcrfilehelper import pcrFileHelper  # noqa: E402

CASES = sys.argv[1:]
for path in CASES:
    print('=' * 70)
    print(path)
    print('=' * 70)
    if not os.path.exists(path):
        print('   MISSING')
        continue
    h = pcrFileHelper()
    try:
        h.readFromPcrFile(path)
        print('   READ OK | refined params:', len(h.param_list))
        p = h.fit.get('Pattern')[0]
        print('   Nba=%s NbaPoint=%s Nex=%s Npr=%s Thmin=%s Thmax=%s'
              % (p.get('Nba'), p.get('NbaPoint'), p.get('Nex'), p.get('Npr'),
                 p.get('Thmin'), p.get('Thmax')))
    except Exception as e:
        print('   FAILED:', type(e).__name__, e)
        for line in traceback.format_exc().splitlines()[-5:]:
            print('   ' + line)
    # also report the background block style so the cause is visible
    txt = open(path, encoding='utf-8', errors='replace').read()
    for tag in ('Chebychev polynomials', 'Polynomial of', 'Background coefficients'):
        if tag in txt:
            print('   bg style: %s' % tag)
