/**
 * Shared design system for the panel.
 *
 * Everything is expressed through Home Assistant's own theme variables, so the panel
 * follows the user's theme — including custom and dark themes — instead of imposing its
 * own palette. Fallbacks are supplied for every variable so the panel still looks
 * deliberate if a theme omits one.
 */

export const TOKENS = /* css */ `
:host {
  /* Surfaces */
  --rv-bg: var(--primary-background-color, #f2f4f7);
  --rv-surface: var(--card-background-color, #fff);
  --rv-surface-raised: var(--ha-card-background, var(--card-background-color, #fff));
  --rv-surface-sunken: var(--secondary-background-color, #e9ecf1);

  /* Text */
  --rv-text: var(--primary-text-color, #1c1e21);
  --rv-text-dim: var(--secondary-text-color, #6b7280);
  --rv-text-on-accent: var(--text-primary-color, #fff);

  /* Lines and accents */
  --rv-line: var(--divider-color, rgba(127, 127, 127, 0.22));
  --rv-accent: var(--primary-color, #03a9f4);
  --rv-error: var(--error-color, #db4437);
  --rv-warn: var(--warning-color, #ffa600);
  --rv-ok: var(--success-color, #43a047);

  /* Trigger tones. Translucent fills adapt to light and dark automatically. */
  --rv-tone-person: #4f7cff;
  --rv-tone-vehicle: #f0a020;
  --rv-tone-animal: #2eaa6e;
  --rv-tone-alert: #e5484d;
  --rv-tone-motion: #8b93a7;
  --rv-tone-neutral: #8b93a7;

  /* Shape and rhythm */
  --rv-radius: 14px;
  --rv-radius-sm: 10px;
  --rv-radius-pill: 999px;
  --rv-gap: 12px;
  --rv-shadow: 0 1px 2px rgba(0, 0, 0, 0.06), 0 4px 14px rgba(0, 0, 0, 0.06);
  --rv-shadow-lifted: 0 6px 24px rgba(0, 0, 0, 0.16);
  --rv-ease: cubic-bezier(0.2, 0, 0.2, 1);
}
`;

export const BASE = /* css */ `
*, *::before, *::after { box-sizing: border-box; }

:host {
  display: block;
  color: var(--rv-text);
  font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, system-ui, sans-serif));
  -webkit-font-smoothing: antialiased;
}

/* ---------------------------------------------------------------- typography */

.h1 { margin: 0; font-size: 1.4rem; font-weight: 600; letter-spacing: -0.01em; }
.h2 { margin: 0; font-size: 1.05rem; font-weight: 600; }
.h3 { margin: 0; font-size: 0.95rem; font-weight: 600; }
.dim { color: var(--rv-text-dim); }
.small { font-size: 0.82rem; }
.tabular { font-variant-numeric: tabular-nums; }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* -------------------------------------------------------------------- layout */

.row { display: flex; align-items: center; gap: var(--rv-gap); }
.row--tight { gap: 6px; }
.row--wrap { flex-wrap: wrap; }
.col { display: flex; flex-direction: column; gap: var(--rv-gap); }
.spacer { flex: 1 1 auto; }

/* --------------------------------------------------------------------- cards */

.card {
  background: var(--rv-surface);
  border-radius: var(--rv-radius);
  box-shadow: var(--rv-shadow);
  border: 1px solid transparent;
}

/* -------------------------------------------------------------------- icons */

.icon { --mdc-icon-size: 20px; width: 20px; height: 20px; flex: 0 0 auto; }
.icon--sm { --mdc-icon-size: 16px; width: 16px; height: 16px; }
.icon--lg { --mdc-icon-size: 28px; width: 28px; height: 28px; }
.icon--missing { display: inline-block; }

/* ------------------------------------------------------------------ buttons */

button {
  font: inherit;
  color: inherit;
  border: none;
  background: none;
  cursor: pointer;
}
button:disabled { cursor: not-allowed; opacity: 0.45; }

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 9px 16px;
  border-radius: var(--rv-radius-pill);
  font-size: 0.9rem;
  font-weight: 500;
  background: var(--rv-surface-sunken);
  transition: background 140ms var(--rv-ease), transform 140ms var(--rv-ease), box-shadow 140ms var(--rv-ease);
}
.btn:hover:not(:disabled) { background: color-mix(in srgb, var(--rv-accent) 12%, var(--rv-surface-sunken)); }
.btn:active:not(:disabled) { transform: scale(0.97); }

.btn--primary { background: var(--rv-accent); color: var(--rv-text-on-accent); }
.btn--primary:hover:not(:disabled) { background: color-mix(in srgb, #000 10%, var(--rv-accent)); }

.btn--quiet { background: transparent; }
.btn--quiet:hover:not(:disabled) { background: color-mix(in srgb, var(--rv-text) 8%, transparent); }

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  color: inherit;
  transition: background 140ms var(--rv-ease);
}
.icon-btn:hover:not(:disabled) { background: color-mix(in srgb, currentColor 12%, transparent); }
/* inline-flex would otherwise beat the browser's own [hidden] rule */
.icon-btn[hidden] { display: none; }

:focus-visible { outline: 2px solid var(--rv-accent); outline-offset: 2px; }

/* -------------------------------------------------------------------- chips */

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: var(--rv-radius-pill);
  font-size: 0.78rem;
  font-weight: 600;
  line-height: 1.4;
  white-space: nowrap;
  background: color-mix(in srgb, var(--chip-tone, var(--rv-tone-neutral)) 16%, transparent);
  color: color-mix(in srgb, var(--chip-tone, var(--rv-tone-neutral)) 82%, var(--rv-text));
}
.chip .icon { --mdc-icon-size: 15px; width: 15px; height: 15px; }

.chip[data-tone="person"] { --chip-tone: var(--rv-tone-person); }
.chip[data-tone="vehicle"] { --chip-tone: var(--rv-tone-vehicle); }
.chip[data-tone="animal"] { --chip-tone: var(--rv-tone-animal); }
.chip[data-tone="alert"] { --chip-tone: var(--rv-tone-alert); }
.chip[data-tone="motion"] { --chip-tone: var(--rv-tone-motion); }
.chip[data-tone="neutral"] { --chip-tone: var(--rv-tone-neutral); }

/* Toggleable filter chips */
.chip--button {
  cursor: pointer;
  border: 1px solid var(--rv-line);
  background: transparent;
  color: var(--rv-text-dim);
  transition: background 140ms var(--rv-ease), color 140ms var(--rv-ease), border-color 140ms var(--rv-ease);
}
.chip--button:hover { border-color: color-mix(in srgb, var(--rv-accent) 40%, var(--rv-line)); }
.chip--button[aria-pressed="true"] {
  border-color: transparent;
  background: color-mix(in srgb, var(--chip-tone, var(--rv-accent)) 20%, transparent);
  color: color-mix(in srgb, var(--chip-tone, var(--rv-accent)) 85%, var(--rv-text));
}

/* ------------------------------------------------------------------ badges */

.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  border: 1px solid var(--rv-line);
  color: var(--rv-text-dim);
}
.badge--strong { border-color: transparent; background: color-mix(in srgb, var(--rv-accent) 18%, transparent); color: color-mix(in srgb, var(--rv-accent) 85%, var(--rv-text)); }
.badge--warn { border-color: transparent; background: color-mix(in srgb, var(--rv-warn) 20%, transparent); color: color-mix(in srgb, var(--rv-warn) 80%, var(--rv-text)); }
.badge--error { border-color: transparent; background: color-mix(in srgb, var(--rv-error) 18%, transparent); color: color-mix(in srgb, var(--rv-error) 85%, var(--rv-text)); }
.badge--ok { border-color: transparent; background: color-mix(in srgb, var(--rv-ok) 18%, transparent); color: color-mix(in srgb, var(--rv-ok) 80%, var(--rv-text)); }

/* --------------------------------------------------------- loading feedback */

.skeleton {
  position: relative;
  overflow: hidden;
  background: color-mix(in srgb, var(--rv-text) 8%, transparent);
  border-radius: 6px;
}
.skeleton::after {
  content: "";
  position: absolute;
  inset: 0;
  transform: translateX(-100%);
  background: linear-gradient(
    90deg,
    transparent,
    color-mix(in srgb, var(--rv-text) 6%, transparent),
    transparent
  );
  animation: rv-shimmer 1.4s infinite;
}
@keyframes rv-shimmer {100% { transform: translateX(100%); } }

.spinner {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid color-mix(in srgb, currentColor 25%, transparent);
  border-top-color: currentColor;
  animation: rv-spin 720ms linear infinite;
  flex: 0 0 auto;
}
@keyframes rv-spin { 100% { transform: rotate(360deg); } }

/* --------------------------------------------------------------- empty state */

.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 56px 24px;
  text-align: center;
  color: var(--rv-text-dim);
}
.empty .icon { --mdc-icon-size: 44px; width: 44px; height: 44px; opacity: 0.5; }
.empty__title { font-size: 1rem; font-weight: 600; color: var(--rv-text); }
.empty__body { max-width: 42ch; line-height: 1.5; font-size: 0.88rem; }

/* ------------------------------------------------------------------ motion */

.enter { animation: rv-enter 260ms var(--rv-ease) both; }
@keyframes rv-enter {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
  }
}

/* --------------------------------------------------------------- scrollbars */

.scroll { overflow: auto; scrollbar-width: thin; overscroll-behavior: contain; }
.scroll::-webkit-scrollbar { width: 10px; height: 10px; }
.scroll::-webkit-scrollbar-thumb {
  background: color-mix(in srgb, var(--rv-text) 22%, transparent);
  border-radius: 8px;
  border: 3px solid transparent;
  background-clip: content-box;
}
.scroll::-webkit-scrollbar-track { background: transparent; }
`;

export const SHARED = TOKENS + BASE;
