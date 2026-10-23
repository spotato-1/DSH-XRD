# -*- coding: utf-8 -*-
"""Target table for the wavelength (λ) feature — LOOKUP ONLY.

SCOPE (user 2026-10-03, after several rounds of design): this module does NOT
read, parse, rank or reconcile wavelength values from files any more. The earlier
resolver (precedence chain + conflict bands + automatic parsing of .pcr/.prm/CIF)
was deleted because its premise was wrong: the authority is whatever file the
USER supplied as the input - format does not decide authority, and a partially
refined .gpx handed over as input is authoritative while our own upstream products
never are.

What remains is the one thing that is genuinely a table: the K-alpha doublet of
each anode, used when no value can be obtained from the user's input and the user
has been asked and does not know. The values are GSAS-II's own table (which cites
Intl. Tables C, Table 4.2.2.1, p. 177-179) so that the numbers agree with the GSAS
engine; note they differ from the classic tabulation this plugin's manual engine
had been defaulting to (Cu 1.54056 vs 1.54051) - using this table is using an
ENGINE DEFAULT and must be reported as such.

CLI:  python wavelength.py --target Cu
"""
import sys

TARGET_TABLE = {
    'Cu': [1.54051, 1.54433], 'Ti': [2.74841, 2.75207], 'Cr': [2.28962, 2.29351],
    'Fe': [1.93597, 1.93991], 'Co': [1.78892, 1.79278], 'Ga': [1.34003, 1.34394],
    'Mo': [0.70926, 0.713543], 'Ag': [0.559363, 0.563775], 'In': [0.512094, 0.516525],
}
# The classic (Intl. Tables) Cu values this plugin's manual engine used to default to.
CLASSIC_CU = (1.54056, 1.54439)
TABLE_SOURCE = 'GSAS-II GSASIIElem.waves (Intl. Tables C, Table 4.2.2.1, p.177-179)'


def defaults_for(target):
    """K-alpha doublet for an anode symbol, or None when it is not in the table.

    A synchrotron source has NO anode: there the wavelength must be supplied by
    the user, and this function returns None for 'synchrotron'/unknown symbols.
    """
    if not target:
        return None
    key = ''.join(ch for ch in str(target) if ch.isalpha())[:2].capitalize()
    if key not in TARGET_TABLE:
        return None
    ka1, ka2 = TARGET_TABLE[key]
    return {
        'target': key,
        'kind': 'doublet',
        'kalpha1': ka1,
        'kalpha2': ka2,
        'ratio': None,                 # not tabulated per anode; engines carry their own
        'source': 'target table (%s)' % TABLE_SOURCE,
        'is_engine_default': True,
        'note': ('table value; the classic tabulation differs (Cu %.5f/%.5f) - '
                 'report which convention was used' % CLASSIC_CU),
    }


def main():
    argv = sys.argv[1:]
    target = None
    if '--target' in argv:
        i = argv.index('--target')
        target = argv[i + 1] if i + 1 < len(argv) else None
    if not target:
        print('usage: python wavelength.py --target <Cu|Mo|Co|Fe|Cr|Ti|Ga|Ag|In>')
        print('known targets:', ', '.join(sorted(TARGET_TABLE)))
        return
    d = defaults_for(target)
    if not d:
        print('%s: not in the target table (synchrotron has no anode - the wavelength '
              'must be supplied by the user)' % target)
        return
    print('%s K-alpha1 = %s A, K-alpha2 = %s A  [%s]'
          % (d['target'], d['kalpha1'], d['kalpha2'], d['source']))


if __name__ == '__main__':
    main()
