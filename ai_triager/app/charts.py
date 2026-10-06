"""Vega-Lite specs for the overview.

Single-series marks use the brand teal (>= 3:1 on light and dark paper), confidence an ordinal
teal ramp validated for both modes with the dataviz palette checker, and review outcomes the
fixed status palette. Backgrounds stay transparent; panel-material-ui switches the Vega theme to
`dark` in dark mode, which recolours text and axes.
"""
from __future__ import annotations

import pandas as pd

SERIES = '#14866f'
CONFIDENCE = {'low': '#66b7a5', 'medium': '#1f9a82', 'high': '#0d5f51'}
REVIEW = {'unreviewed': '#a8a7a1', 'verified as-is': '#0ca30c', 'verified with edits': '#fab219',
          'disputed': '#d03b3b'}
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"


def _base(height: int, values: list[dict], **spec) -> dict:
    return {
        '$schema': 'https://vega.github.io/schema/vega-lite/v5.json',
        'background': 'transparent',
        'width': 'container',
        'height': height,
        'data': {'values': values},
        'config': {
            'font': FONT,
            'view': {'stroke': None},
            'axis': {'labelFontSize': 11, 'titleFontSize': 11, 'titleFontWeight': 500, 'domain': False,
                     'ticks': False, 'gridOpacity': 0.5, 'labelPadding': 6},
            'legend': {'labelFontSize': 11, 'titleFontSize': 11, 'orient': 'top', 'title': None,
                       'symbolType': 'square'},
            'bar': {'cornerRadiusEnd': 4},
        },
        **spec,
    }


def categories(counts: pd.Series, descriptions: dict[str, str]) -> dict:
    """Horizontal bars per category, largest first. Clicking selects a category.

    Counts are part of the axis labels rather than text marks: Vega's dark theme recolours axes
    but not text marks, so end-of-bar labels would turn unreadable in dark mode.
    """
    values = [{'category': c, 'label': f'{c}  {int(counts.get(c, 0)):,}', 'count': int(counts.get(c, 0)),
               'meaning': d} for c, d in descriptions.items()]
    return _base(
        max(26 * len(values), 120), values,
        params=[{'name': 'pick', 'select': {'type': 'point', 'fields': ['category'], 'toggle': False,
                                            'on': 'click', 'clear': False}}],
        mark={'type': 'bar', 'color': SERIES, 'cursor': 'pointer', 'height': {'band': 0.7}},
        encoding={
            'y': {'field': 'label', 'type': 'nominal', 'sort': '-x', 'title': None, 'axis': {'labelLimit': 200}},
            'x': {'field': 'count', 'type': 'quantitative', 'title': None, 'axis': {'grid': True, 'tickCount': 4}},
            'tooltip': [{'field': 'category'}, {'field': 'count', 'format': ','}, {'field': 'meaning'}],
        },
    )


def progress(dates: pd.Series) -> dict:
    """Cumulative number of finished writeups by the day they were triaged."""
    daily = pd.to_datetime(dates.dropna(), errors='coerce').dropna().dt.strftime('%Y-%m-%d').value_counts().sort_index()
    values = [{'date': d, 'triaged': int(n)} for d, n in daily.items()]
    return _base(
        180, values,
        transform=[{'sort': [{'field': 'date'}], 'window': [{'op': 'sum', 'field': 'triaged', 'as': 'total'}],
                    'frame': [None, 0]}],
        mark={'type': 'area', 'line': {'color': SERIES, 'strokeWidth': 2}, 'color': SERIES, 'opacity': 0.18,
              'point': {'color': SERIES, 'size': 40}, 'interpolate': 'step-after'},
        encoding={
            'x': {'field': 'date', 'type': 'temporal', 'title': None, 'axis': {'format': '%b %d', 'grid': False}},
            'y': {'field': 'total', 'type': 'quantitative', 'title': None, 'axis': {'tickCount': 4}},
            'tooltip': [{'field': 'date', 'type': 'temporal', 'format': '%Y-%m-%d'},
                        {'field': 'triaged', 'title': 'triaged that day'},
                        {'field': 'total', 'title': 'total so far', 'format': ','}],
        },
    )


def recommendations(final: pd.DataFrame) -> dict:
    """Recommendation counts, stacked by the agent's confidence."""
    grouped = final.groupby(['recommendation', 'confidence']).size()
    rank = {c: i for i, c in enumerate(CONFIDENCE)}
    values = [{'recommendation': r, 'confidence': c, 'count': int(n), 'rank': rank.get(c, -1)}
              for (r, c), n in grouped.items()]
    return _base(
        170, values,
        mark={'type': 'bar', 'height': {'band': 0.7}, 'stroke': 'transparent', 'strokeWidth': 2},
        encoding={
            'y': {'field': 'recommendation', 'type': 'nominal', 'sort': '-x', 'title': None},
            'x': {'aggregate': 'sum', 'field': 'count', 'type': 'quantitative', 'title': None,
                  'axis': {'grid': True, 'tickCount': 4}},
            'color': {'field': 'confidence', 'type': 'ordinal', 'sort': list(CONFIDENCE),
                      'scale': {'domain': list(CONFIDENCE), 'range': list(CONFIDENCE.values())},
                      'legend': {'title': None}},
            'order': {'field': 'rank', 'sort': 'ascending'},
            'tooltip': [{'field': 'recommendation'}, {'field': 'confidence'},
                        {'aggregate': 'sum', 'field': 'count', 'title': 'writeups'}],
        },
    )


def review_status(stats: pd.DataFrame, limit: int = 6) -> dict:
    """Per model, how many writeups are unreviewed, verified as-is, verified with edits or disputed."""
    stats = stats.head(limit)
    values = []
    for row in stats.itertuples():
        parts = {'unreviewed': row.triaged - row.reviewed, 'verified as-is': row.as_is,
                 'verified with edits': row.edited, 'disputed': row.disputed}
        rate = '' if pd.isna(row.agreement) else f'{row.agreement:.0%}'
        values += [{'model': row.model, 'status': s, 'count': int(n), 'as-is rate': rate, 'order': i}
                   for i, (s, n) in enumerate(parts.items())]
    return _base(
        max(34 * len(stats), 100), values,
        mark={'type': 'bar', 'height': {'band': 0.65}},
        encoding={
            'y': {'field': 'model', 'type': 'nominal', 'sort': '-x', 'title': None, 'axis': {'labelLimit': 240}},
            'x': {'aggregate': 'sum', 'field': 'count', 'type': 'quantitative', 'title': None,
                  'axis': {'grid': True, 'tickCount': 4}},
            'color': {'field': 'status', 'type': 'nominal', 'sort': list(REVIEW),
                      'scale': {'domain': list(REVIEW), 'range': list(REVIEW.values())}},
            'order': {'field': 'order'},
            'tooltip': [{'field': 'model'}, {'field': 'status'}, {'field': 'count', 'format': ','},
                        {'field': 'as-is rate'}],
        },
    )


TRIAGE_STATE = {'triaged': SERIES, 'not triaged': '#a8a7a1'}


def issue_types(rows: list[dict]) -> dict:
    """Open issues per GitHub issue type, split by whether they have a finished writeup."""
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row['type'] or 'no type', 'triaged' if row['triaged'] else 'not triaged')
        counts[key] = counts.get(key, 0) + 1
    totals: dict[str, int] = {}
    for (kind, _), n in counts.items():
        totals[kind] = totals.get(kind, 0) + n
    order = list(TRIAGE_STATE)
    values = [{'type': f'{kind}  {totals[kind]:,}', 'name': kind, 'state': state, 'count': n,
               'order': order.index(state)} for (kind, state), n in counts.items()]
    return _base(
        max(30 * len(totals), 100), values,
        mark={'type': 'bar', 'height': {'band': 0.7}, 'stroke': 'transparent', 'strokeWidth': 2},
        encoding={
            'y': {'field': 'type', 'type': 'nominal', 'sort': '-x', 'title': None, 'axis': {'labelLimit': 200}},
            'x': {'aggregate': 'sum', 'field': 'count', 'type': 'quantitative', 'title': None,
                  'axis': {'grid': True, 'tickCount': 4}},
            'color': {'field': 'state', 'type': 'nominal', 'sort': order,
                      'scale': {'domain': order, 'range': list(TRIAGE_STATE.values())}},
            'order': {'field': 'order'},
            'tooltip': [{'field': 'name', 'title': 'type'}, {'field': 'state'}, {'field': 'count', 'format': ','}],
        },
    )
