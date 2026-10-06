"""Theme and shared building blocks for the app."""
from __future__ import annotations

import functools

from pathlib import Path

import panel as pn

SANS = "system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"
MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
NBSP = '\N{NO-BREAK SPACE}'

ASSETS = Path(__file__).parent / 'assets'

TEAL, TEAL_DARK = '#14866f', '#38c2a5'
CORAL, CORAL_DARK = '#e8743b', '#ff9a62'

_TYPOGRAPHY = {
    'fontFamily': SANS,
    'fontSize': 14,
    'h5': {'fontWeight': 700, 'letterSpacing': '-0.01em'},
    'h6': {'fontSize': '1.05rem', 'fontWeight': 650, 'letterSpacing': '-0.005em'},
    'subtitle1': {'fontWeight': 600},
    'body2': {'lineHeight': 1.6},
    'caption': {'fontSize': '0.78rem', 'lineHeight': 1.6},
    'overline': {'fontSize': '0.68rem', 'fontWeight': 650, 'letterSpacing': '0.09em', 'lineHeight': 1.8},
    'button': {'textTransform': 'none', 'fontWeight': 600},
    'kpi': {'fontFamily': MONO, 'fontSize': '1.6rem', 'fontWeight': 650,
            'fontVariantNumeric': 'tabular-nums', 'whiteSpace': 'nowrap', 'lineHeight': 1.3},
    'data': {'fontFamily': MONO, 'fontSize': '0.85rem', 'fontVariantNumeric': 'tabular-nums'},
}
_COMPONENTS = {
    'MuiButton': {'defaultProps': {'disableElevation': True},
                  'styleOverrides': {'root': {'borderRadius': 10, 'paddingInline': 14}}},
    'MuiChip': {'styleOverrides': {'root': {'fontWeight': 500}}},
    'MuiPaper': {'styleOverrides': {'outlined': {'borderRadius': 14}}},
    'MuiListItemButton': {'styleOverrides': {'root': {'borderRadius': 10, 'marginInline': 8}}},
}


def _mode(primary: str, secondary: str, background: str, paper: str, text: str, muted: str, divider: str) -> dict:
    return {
        'palette': {
            'primary': {'main': primary},
            'secondary': {'main': secondary},
            'success': {'main': '#0ca30c'},
            'warning': {'main': '#c98500'},
            'error': {'main': '#d03b3b'},
            'background': {'default': background, 'paper': paper},
            'text': {'primary': text, 'secondary': muted},
            'divider': divider,
            'contrastThreshold': 4.5,
        },
        'typography': _TYPOGRAPHY,
        'shape': {'borderRadius': 12},
        'components': _COMPONENTS,
    }


# Calm and friendly: a "merged" teal with a warm coral accent on warm paper, soft charcoal in dark mode.
THEME = {
    'light': _mode(TEAL, CORAL, '#f6f5f1', '#ffffff', '#1f2328', '#59636e', 'rgba(31, 35, 40, 0.12)'),
    'dark': _mode(TEAL_DARK, CORAL_DARK, '#0f1214', '#171b1f', '#e6edf3', '#9198a1', 'rgba(230, 237, 243, 0.12)'),
}

HEADER_SX = {'& .header': {
    'backgroundImage': f'linear-gradient(100deg, #0f6f5d 0%, {TEAL} 55%, #1f9a82 100%)',
    'boxShadow': 'none',
}}

PAPER_SX = {'p': 2, 'height': '100%'}


def section(title: str, *objects, subtitle: str = '', **kwargs) -> pn.ui.Paper:
    """An outlined Paper with a heading, the building block of every page."""
    header = [pn.ui.Typography(title, variant='h6', margin=0)]
    if subtitle:
        header.append(pn.ui.Typography(subtitle, variant='caption', margin=0, sx={'color': 'text.secondary'}))
    return pn.ui.Paper(
        *header, *objects, variant='outlined', margin=0, sizing_mode='stretch_width',
        sx={**PAPER_SX, 'display': 'flex', 'flexDirection': 'column', 'gap': '8px', **kwargs.pop('sx', {})},
        **kwargs,
    )


class KPI:
    """A KPI card built once; `update` changes its value and note in place."""

    def __init__(self, label: str):
        self.value = pn.ui.Typography(NBSP, variant='kpi', margin=0)
        self.note = pn.ui.Typography(NBSP, variant='caption', margin=0, sx={'color': 'text.secondary'})
        self.card = pn.ui.Paper(
            pn.ui.Typography(label, variant='overline', margin=0, sx={'color': 'text.secondary'}),
            self.value, self.note, variant='outlined', margin=0, sizing_mode='stretch_width', sx=PAPER_SX,
        )

    def update(self, value, note: str = ''):
        self.value.object = str(value)
        self.note.object = note or NBSP


def held(method):
    """Batch all widget updates a method makes into one redraw."""
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        with pn.io.hold():
            return method(*args, **kwargs)
    return wrapper


def resizable(left, right, *, sizes=(50, 50), min_size=(320, 320), height: str = '', margin=0):
    """A list/detail pair with a draggable divider; the pair fills ``height`` and each side scrolls itself."""
    from panel_splitjs import HSplit

    return HSplit(left, right, sizes=sizes, min_size=min_size, gutter_size=10, sizing_mode='stretch_width',
                  margin=margin, styles={'height': height} if height else {}, stylesheets=[SPLIT_CSS])


# split.js sizes the panes but leaves the wrapper's height to its content; make the pair fill the host so
# each side gets the full height and scrolls internally.
SPLIT_CSS = '''
.hsplit, .split-panel { height: 100%; }
.split { background-color: transparent; }
.split-panel { min-width: 0; overflow: hidden; }
'''
