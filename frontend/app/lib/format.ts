// Amounts travel over the wire as integers in minor units (2500 === $25.00).
// Formatting is a presentation concern and happens only here.

const SYMBOLS: Record<string, string> = { USD: "$", EUR: "€", GBP: "£" };

export function formatAmount(minorUnits: number, currency: string): string {
  const symbol = SYMBOLS[currency] ?? "";
  const sign = minorUnits < 0 ? "-" : "";
  const major = (Math.abs(minorUnits) / 100).toFixed(2);
  const grouped = major.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}${symbol}${grouped}${symbol ? "" : ` ${currency}`}`;
}

export function formatDate(iso: string): string {
  // Pinned locale: the browser default would render differently per visitor
  // and would not match what the server prerendered.
  return new Date(iso).toLocaleDateString("en-GB", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
