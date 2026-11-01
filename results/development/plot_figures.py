"""Replot the three retained figures from public CSVs only.

Requires numpy and matplotlib. Supply a new output directory; original figures
are never overwritten. This plots existing metrics, not simulation trajectories.
"""
import argparse
import csv
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

DATA = Path(__file__).resolve().parent


def read(name):
    with (DATA / name).open() as source:
        return list(csv.DictReader(source))


def pilot(destination):
    rows = read('pilot_speed_cells.csv')
    matrices = {(q, s): np.full((22, 140), np.nan)
                for q in (750, 800, 850, 900, 950) for s in (17, 23, 42)}
    for row in rows:
        if row['speed_mps']:
            matrices[int(row['nominal_R_veh_h']), int(row['seed'])][
                int(row['cell']), int(row['begin']) // 30] = float(row['speed_mps'])
    fig, axes = plt.subplots(5, 3, figsize=(15, 17), sharex=True, sharey=True,
                             constrained_layout=True)
    cmap = plt.get_cmap('viridis').copy()
    cmap.set_bad('#dedede')
    for i, q in enumerate((750, 800, 850, 900, 950)):
        for j, seed in enumerate((17, 23, 42)):
            ax = axes[i, j]
            mesh = ax.pcolormesh(np.arange(0, 4201, 30), np.arange(0, 2201, 100),
                                 matrices[q, seed], cmap=cmap, vmin=0, vmax=45,
                                 shading='flat', rasterized=True)
            for t, style in ((600, '--'), (1200, ':'), (3000, '--')):
                ax.axvline(t, color='white', lw=.75, ls=style, alpha=.85)
            for x in (1400, 1696):
                ax.axhline(x, color='black', lw=.6, ls=':', alpha=.7)
            ax.set_title(f'Nominal R{q} | seed {seed}', fontsize=10)
            ax.set_xlim(0, 4200)
            ax.set_ylim(0, 2200)
            ax.set_xticks([0, 600, 1200, 2400, 3000, 4200])
            ax.tick_params(labelsize=8)
            if j == 0:
                ax.set_ylabel('Mainline path position (m)')
            if i == 4:
                ax.set_xlabel('Simulation time (s)')
    fig.colorbar(mesh, ax=axes, label='M vehicle-second weighted speed (m/s)',
                 shrink=.85, pad=.015)
    fig.suptitle('Exploratory OPEN Pilot | M3600 | full 0–4200 s\n'
                 'R loading 600–3000 s; diagnostic window 1200–3000 s; black dotted: merge 1400–1696 m\n'
                 '100 m × 30 s cells; two mainline lanes including internal connectors; grey = no M samples', fontsize=12)
    fig.savefig(destination / 'demand_location.png', dpi=180)
    plt.close(fig)


def costs(destination):
    controlled = {(r['target_occupancy_percent'], int(r['seed'])): r
                  for r in read('alinea_sensitivity.csv')}
    reference = {int(r['seed']): r for r in read('demand_selection.csv')
                 if r['nominal_q_ramp_veh_h'] == '900'}
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), constrained_layout=True)
    for ax, role in zip(axes.flat, 'MRUX'):
        for seed, color in [(17, '#0072B2'), (23, '#D55E00'), (42, '#009E73')]:
            values = [float(reference[seed][role + '_system_time_mean_s'])]
            values += [float(controlled[target, seed][role + '_system_mean_per_planned_s'])
                       for target in ('9', '11', '13')]
            ax.plot(range(4), values, '-o', color=color, label=f'seed {seed}')
        ax.set_xticks(range(4), ['OPEN', '9%', '11%', '13%'])
        ax.set_ylabel('Observed system time / planned vehicle (s)')
        ax.set_title(role + ' cohort (network + source wait)')
        ax.grid(alpha=.2)
    axes[0, 0].legend()
    fig.suptitle('Development R900 | paired seeds | all planned vehicles | finite horizon 0–4200s')
    fig.savefig(destination / 'alinea_costs.png', dpi=180)
    plt.close(fig)


def queue_protection(destination):
    rows = read('qp900_timeseries.csv')
    t = np.array([int(r['time_s']) for r in rows])
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True, constrained_layout=True)
    for key, label, style in [('qp900_nominal_rate_veh_h', 'QP run: nominal ALINEA', '-'),
                               ('qp900_final_rate_veh_h', 'QP run: final request', '-'),
                               ('alinea13_nominal_rate_veh_h', 'Pure ALINEA reference', '--')]:
        axes[0].plot(t, [float(r[key]) for r in rows], label=label,
                     ls=style, lw=.8 if style == '--' else 1)
    axes[0].set_ylabel('Requested rate (veh/h)')
    axes[0].legend(fontsize=8)
    axes[1].plot(t, [float(r['qp900_risk_extent_m']) for r in rows],
                 label='QP run: observed PRE risk', lw=1)
    axes[1].plot(t, [float(r['alinea13_risk_extent_m']) for r in rows],
                 label='Pure ALINEA: same risk metric', lw=.8, alpha=.7)
    for limit in (261.03, 102.245):
        axes[1].axhline(limit, color='black', ls=':', lw=.7)
    axes[1].set_ylabel('Slow R rear extent (m)')
    axes[1].legend(fontsize=8)
    passages = read('qp900_meter_passages.csv')
    for rid, label in [('DEV_20261028_M3600_R900_ALINEA_O13_S17', 'Pure ALINEA13'),
                       ('DEV_20261029_M3600_R900_ALINEA_QP_O13_S17_V2', 'ALINEA13 + QP')]:
        counts = np.zeros(len(t))
        for event in passages:
            second = int(event['time_begin_s'])
            if event['run_id'] == rid and 600 <= second < 4200:
                counts[second - 600] += 1
        axes[2].step(t, np.cumsum(counts), where='post', label=label)
    axes[2].set_ylabel('Cumulative actual meter N (veh)')
    axes[2].set_xlabel('Simulation time (s)')
    axes[2].legend(fontsize=8)
    for ax in axes:
        ax.set_xlim(600, 4200)
        ax.grid(alpha=.2)
        for v in (1200, 3000):
            ax.axvline(v, color='grey', ls='--', lw=.7)
        ax.fill_between(t, 0, 1, where=[r['qp900_active'] == 'True' for r in rows],
                        transform=ax.get_xaxis_transform(), color='#e6ab02', alpha=.12)
    fig.suptitle('One development pair | M3600 R900 seed17 | shaded: actual QP active\n'
                 'Risk = farthest slow R rear on storage/internal lanes, not continuous queue length', fontsize=12)
    fig.savefig(destination / 'qp900_mechanism.png', dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    destination = parser.parse_args().output_dir
    destination.mkdir(parents=True, exist_ok=False)
    pilot(destination)
    costs(destination)
    queue_protection(destination)
    print(f'Wrote three figures to {destination}')


if __name__ == '__main__':
    main()
