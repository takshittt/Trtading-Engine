export type Theme = 'light' | 'dark'

/**
 * Applied to <html> as data-theme, which is what index.css remaps the palette
 * on. The dashboard is dark-only (matching the Gateway UI), so this is always
 * called with 'dark' from main.tsx before React mounts.
 */
export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
}
