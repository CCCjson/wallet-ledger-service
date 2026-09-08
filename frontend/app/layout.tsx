import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Daely Wallet",
  description: "Wallet balances and transaction history",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
