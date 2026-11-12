// One cache for Intl.DateTimeFormat instances, which are costly to build and were being cached by
// hand in five places (#112). Keyed by locale and options; the same formatter comes back each time.

const cache = new Map<string, Intl.DateTimeFormat>()

export function dateTimeFormat(
  locale: string,
  options: Intl.DateTimeFormatOptions,
): Intl.DateTimeFormat {
  const key = `${locale}|${JSON.stringify(options)}`
  let format = cache.get(key)
  if (!format) {
    format = new Intl.DateTimeFormat(locale, options)
    cache.set(key, format)
  }
  return format
}
