export function isWindowsPath(value = '') {
  const path = String(value || '').trim();
  return /^[a-zA-Z]:([\\/]|$)/.test(path) || /^\\\\/.test(path);
}

export function parentPath(value = '') {
  const raw = String(value || '').trim();
  if (!raw) return '';

  if (isWindowsPath(raw)) {
    const path = raw.replaceAll('/', '\\');
    const drive = path.match(/^([a-zA-Z]:)(?:\\.*)?$/);
    const unc = path.match(/^(\\\\[^\\]+\\[^\\]+)/);
    const floor = drive ? `${drive[1]}\\` : (unc?.[1] || '\\\\');
    const trimmed = path.length > floor.length ? path.replace(/\\+$/, '') : path;
    if (trimmed.length <= floor.length) return floor;
    const splitAt = trimmed.lastIndexOf('\\');
    if (splitAt < floor.length) return floor;
    return trimmed.slice(0, splitAt) || floor;
  }

  const trimmed = raw.length > 1 ? raw.replace(/\/+$/, '') : raw;
  if (trimmed === '/') return '/';
  const splitAt = trimmed.lastIndexOf('/');
  return splitAt > 0 ? trimmed.slice(0, splitAt) : '/';
}

export function joinPath(baseValue = '', nameValue = '') {
  const base = String(baseValue || '').trim();
  const name = String(nameValue || '').replace(/^[\\/]+/, '');
  if (!base) return name;
  const separator = isWindowsPath(base) ? '\\' : '/';
  const trimmedBase = base.replace(/[\\/]+$/, '');
  return `${trimmedBase}${separator}${name}`;
}
