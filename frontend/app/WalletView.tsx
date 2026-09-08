"use client";

import { useEffect, useState } from "react";
import { formatAmount, formatDate } from "./lib/format";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const DEBOUNCE_MS = 300;

type Balance = { currency: string; balance: number };
type Transaction = {
  id: number;
  event_id: string;
  amount: number;
  currency: string;
  type: "top_up" | "payment";
  created_at: string;
};
type Wallet = {
  user_id: string;
  balances: Balance[];
  transactions: Transaction[];
  query_understood: boolean;
};

export default function WalletView({ userId }: { userId: string }) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Debounce so that typing does not fire one LLM call per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const url = new URL(`/wallets/${encodeURIComponent(userId)}`, API_URL);
        if (debounced.trim()) url.searchParams.set("q", debounced.trim());

        const res = await fetch(url, { signal: controller.signal });
        if (!res.ok) throw new Error(`Request failed with status ${res.status}`);
        setWallet(await res.json());
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        setError((err as Error).message || "Something went wrong");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    load();
    return () => controller.abort();
  }, [userId, debounced]);

  // Balances come from the wallet, never from the filtered transaction list, so
  // a search narrows the history without changing what the user is worth.
  const balances = wallet?.balances ?? [];

  // A request that fails when there is already a wallet on screen is a failed
  // *search*. The balances and the previous list are still true, so the page
  // keeps showing them and explains what happened; wiping them would be the
  // search changing what the user is worth, which is the one thing it must
  // never do. Only a failure with nothing on screen is a whole-page failure.
  const hardError = error !== null && wallet === null;
  const softError = error !== null && wallet !== null;

  return (
    <>
      <h2>Balances</h2>
      {hardError ? (
        <div className="state error">Balances unavailable.</div>
      ) : loading && !wallet ? (
        <div className="state">Loading balances…</div>
      ) : balances.length === 0 ? (
        <div className="state">No balances yet.</div>
      ) : (
        <div className="balances">
          {balances.map((b) => (
            <div className="balance" key={b.currency}>
              <div className="currency">{b.currency}</div>
              <div className="value">{formatAmount(b.balance, b.currency)}</div>
            </div>
          ))}
        </div>
      )}

      <h2>Transactions</h2>
      <input
        type="search"
        value={query}
        // Matches MAX_QUERY_LENGTH on the API, which rejects anything longer
        // with a 422. Stopping it at the keystroke means the user never sees a
        // failed request for something the browser could simply not allow.
        maxLength={200}
        placeholder="Search, e.g. payments over $50"
        aria-label="Search transactions"
        onChange={(e) => setQuery(e.target.value)}
      />
      <p className="hint">
        Try “payments over $50”, “anything from last week”, or “top ups in euros”.
      </p>

      {softError && (
        <p className="banner">
          Search failed — showing your previous results.
        </p>
      )}

      {!softError && wallet && !wallet.query_understood && debounced.trim() && (
        <p className="banner">
          Couldn’t understand “{debounced.trim()}” — showing all transactions instead.
        </p>
      )}

      <div style={{ marginTop: 16 }}>
        {hardError ? (
          <div className="state error">
            Couldn’t load this wallet: {error}
          </div>
        ) : loading ? (
          <div className="state">Loading transactions…</div>
        ) : !wallet || wallet.transactions.length === 0 ? (
          <div className="state">
            {debounced.trim()
              ? "No transactions match that search."
              : "No transactions yet."}
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Type</th>
                <th style={{ textAlign: "right" }}>Amount</th>
              </tr>
            </thead>
            <tbody>
              {wallet.transactions.map((t) => (
                <tr key={t.id}>
                  <td>{formatDate(t.created_at)}</td>
                  <td>
                    <span className="tag">
                      {t.type === "top_up" ? "Top up" : "Payment"}
                    </span>
                  </td>
                  <td className={`amount ${t.type === "top_up" ? "in" : "out"}`}>
                    {t.type === "top_up" ? "+" : "−"}
                    {formatAmount(t.amount, t.currency)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
