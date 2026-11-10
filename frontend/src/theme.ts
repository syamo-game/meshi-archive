import { defineTheme } from '@astryxdesign/core/theme';
import { neutralTheme } from '@astryxdesign/theme-neutral';

// Brand color and font live in theme tokens so components need no CSS overrides.
export const meshiTheme = defineTheme({
  name: 'meshi',
  extends: neutralTheme,
  typography: { body: { family: 'Meshi Noto Sans JP', fallbacks: 'sans-serif' } },
  tokens: {
    '--color-accent': '#0066d6',
    '--color-text-accent': '#0066d6',
    '--color-icon-accent': '#0066d6',
    '--color-on-accent': '#ffffff',
    '--color-background-body': '#fafbfc',
  },
});
