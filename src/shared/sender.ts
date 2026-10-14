// The address remains the site's verified address; only the display name is personalized.
export function senderFrom(base: string, nickname: string) {
  if (!nickname) return base;
  const address = base.match(/<([^>]+)>/)?.[1] || base.trim();
  return `"${nickname} · 柿间邮局" <${address}>`;
}

export function senderDisplay(from: string) {
  return from.replace(/^"([^"]+)"(?=\s*<)/, '$1');
}
