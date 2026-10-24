#!/usr/bin/env python3
"""Independently verify the six archived exploratory runs and optional replays."""
import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
import xml.etree.ElementTree as ET

RUNS = tuple(f'{arm}_s{seed}' for seed in (17, 23, 42) for arm in ('open', 'v15'))
COUNTS = {'M': 3000, 'R': 600, 'U': 300, 'X': 150}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def xml_signature(path):
    """Hash ordered element events, attributes and non-whitespace text."""
    digest = hashlib.sha256()
    counts = Counter()
    wall = hashlib.sha256()
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as stream:
        for event, element in ET.iterparse(stream, events=('start', 'end')):
            attrs = dict(element.attrib)
            if path.name == 'sumo_summary.xml' and element.tag == 'step':
                value = attrs.pop('duration', None)
                if event == 'start':
                    wall.update(json.dumps(value).encode())
            if event == 'start':
                counts[element.tag] += 1
                payload = [event, element.tag, sorted(attrs.items())]
            else:
                payload = [event, element.tag, (element.text or '').strip(), (element.tail or '').strip()]
            digest.update((json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
            if event == 'end':
                element.clear()
    return {'sha256': digest.hexdigest(), 'element_counts': dict(sorted(counts.items())),
            'wall_duration_sha256': wall.hexdigest() if path.name == 'sumo_summary.xml' else None}


def csv_signature(path):
    digest = hashlib.sha256()
    with path.open(newline='') as stream:
        reader = csv.reader(stream)
        header = next(reader)
        require(len(header) == len(set(header)), 'duplicate CSV columns')
        digest.update(json.dumps(header).encode())
        count = 0
        for row in reader:
            require(len(row) == len(header), 'CSV row width mismatch')
            digest.update(('\n' + json.dumps(row)).encode())
            count += 1
    return {'sha256': digest.hexdigest(), 'columns': header, 'rows': count}


def records(path, tag):
    result = {}
    for _, element in ET.iterparse(path, events=('end',)):
        if element.tag == tag:
            row = dict(element.attrib)
            require('id' in row and row['id'] not in result, f'duplicate or missing {tag} ID')
            result[row['id']] = row
        element.clear()
    return result


def cohort(output, demand, expected=COUNTS):
    planned = records(demand, 'vehicle')
    trips = records(output / 'tripinfo.xml', 'tripinfo')
    routes = records(output / 'vehroute.xml', 'vehicle')
    require(set(planned) == set(trips) == set(routes), 'planned/tripinfo/vehroute ID sets differ')
    counts = Counter(key.split('_')[0] for key in planned)
    require(dict(counts) == expected, f'planned class counts differ: {dict(counts)}')
    totals = {c: {'requested': n, 'system_seconds': Decimal(0), 'source_wait_seconds': Decimal(0)}
              for c, n in expected.items()}
    for key, plan in planned.items():
        trip, route = trips[key], routes[key]
        p, d, a = map(Decimal, (plan['depart'], trip['depart'], trip['arrival']))
        require(0 <= p <= d <= a <= 4200, 'unfinished or invalid vehicle lifecycle')
        require(not trip.get('vaporized'), 'vaporized vehicle')
        require(a - d == Decimal(trip['duration']), 'trip duration inconsistency')
        require(d - p == Decimal(trip['departDelay']), 'source wait inconsistency')
        require(Decimal(route['depart']) == d and Decimal(route['arrival']) == a, 'route/trip lifecycle inconsistency')
        row = totals[key.split('_')[0]]
        row['system_seconds'] += a - p
        row['source_wait_seconds'] += d - p
    return {c: {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()} for c, row in totals.items()}


def verify(archive, inputs, paired, replay=None):
    manifest = json.loads((archive / 'MANIFEST.json').read_text())
    entries = manifest['files']
    require(len(entries) == 123, 'archive file count must be 123')
    require(len({r['copy'] for r in entries}) == 123, 'duplicate manifest paths')
    require(Counter(Path(r['copy']).parts[0] for r in entries) ==
            Counter({run: 18 if run.startswith('open') else 23 for run in RUNS}), 'run coverage mismatch')
    report = {'scope': 'Historical exploratory replay; no formal experiment inference',
              'xml_exceptions': ['Generated XML comments and whitespace formatting',
                                 'sumo_summary.xml step@duration only: wall-clock execution time'],
              'files': [], 'cohorts': {}, 'replay_cohorts': {}, 'paired_rows': 0}
    for entry in entries:
        rel = Path(entry['copy'])
        require(not rel.is_absolute() and '..' not in rel.parts, 'unsafe manifest path')
        source = archive / rel
        require(hashlib.sha256(source.read_bytes()).hexdigest() == entry['copy_sha256'], f'archive checksum differs: {rel}')
        signature = csv_signature if source.suffix == '.csv' else xml_signature
        original = signature(source)
        record = {'file': str(rel), 'archive': original}
        if replay:
            other = signature(replay / rel.parts[0] / 'outputs' / rel.name)
            record['replay'] = other
            require(original['sha256'] == other['sha256'], f'scientific records differ: {rel}')
            if source.name == 'sumo_summary.xml':
                record['wall_duration_differs'] = original['wall_duration_sha256'] != other['wall_duration_sha256']
        report['files'].append(record)
    for seed in (17, 23, 42):
        left = records(inputs / f'open_s{seed}' / 'demand.rou.xml', 'vehicle')
        right = records(inputs / f'v15_s{seed}' / 'demand.rou.xml', 'vehicle')
        require(left == right, f'paired demand attributes differ: seed {seed}')
    for run in RUNS:
        demand = inputs / run / 'demand.rou.xml'
        report['cohorts'][run] = cohort(archive / run, demand)
        if replay:
            report['replay_cohorts'][run] = cohort(replay / run / 'outputs', demand)
            require(report['cohorts'][run] == report['replay_cohorts'][run], f'replay cohort differs: {run}')
    with paired.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    keys = [(r['seed'], r['class']) for r in rows]
    require(len(keys) == 12 and len(set(keys)) == 12 and set(keys) ==
            {(str(s), c) for s in (17, 23, 42) for c in COUNTS}, 'paired table coverage mismatch')
    for row in rows:
        seed, cls = row['seed'], row['class']
        require(int(row['requested']) == COUNTS[cls], 'paired requested count mismatch')
        for metric in ('system_seconds', 'source_wait_seconds'):
            for arm in ('open', 'v15'):
                actual = report['cohorts'][f'{arm}_s{seed}'][cls][metric]
                require(Decimal(row[f'{arm}_{metric}']) == Decimal(actual), f'paired metric differs: {seed}/{cls}/{arm}/{metric}')
        delta = Decimal(row['v15_system_seconds']) - Decimal(row['open_system_seconds'])
        require(delta == Decimal(row['v15_minus_open_system_seconds']), 'paired difference mismatch')
    report['paired_rows'] = len(rows)
    report['status'] = 'PASS_REPLAY_AND_ARCHIVE' if replay else 'PASS_ARCHIVE_ONLY'
    return report


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-root', type=Path, default=root / 'data/exploratory_stage6')
    parser.add_argument('--inputs-root', type=Path, default=root / 'config/reproduction')
    parser.add_argument('--paired-table', type=Path, default=root / 'results/paired_results.csv')
    parser.add_argument('--replay-root', type=Path)
    parser.add_argument('--report', type=Path, required=True, help='New JSON file; existing files are never overwritten')
    args = parser.parse_args()
    require(not args.report.exists(), 'report already exists')
    try:
        result = verify(args.archive_root, args.inputs_root, args.paired_table, args.replay_root)
    except (ValueError, OSError, ET.ParseError, KeyError, StopIteration) as exc:
        result = {'status': 'FAIL', 'error': str(exc)}
    with args.report.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(result['status'])
    return 1 if result['status'] == 'FAIL' else 0


if __name__ == '__main__':
    raise SystemExit(main())
