/** Byte count → human string ("512 B", "3 KB", "1.2 MB", "4.7 GB"). */
export function formatBytes(bytes: number): string {
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)} GB`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

/**
 * Parse a backend timestamp as UTC. Depending on the SQLAlchemy/pydantic
 * versions installed, `created_at` arrives either naive
 * ("2026-10-16T16:30:41.768635") or with a zone suffix ("…Z" / "…+00:00").
 * Appending "Z" to the latter would make an invalid date, so only add it
 * when no zone is present.
 */
export function parseUtc(iso: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : iso + "Z");
}
