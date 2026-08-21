"use client";

import { useEffect, useState } from "react";
import { whoami } from "@/lib/api";

/**
 * Today's spend against the daily budget, in the header.
 *
 * Visible on every page on purpose: an agent that costs money should show
 * what it costs while you are deciding whether to ask it another question.
 */
export function Spend() {
  const [text, setText] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      whoami()
        .then((me) => {
          if (!cancelled) {
            setText(
              `${me.sub} · $${Number(me.spent_today_usd).toFixed(2)} / $${me.daily_budget_usd.toFixed(0)} today`,
            );
          }
        })
        .catch(() => {
          if (!cancelled) setText("");
        });

    load();
    const timer = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return <div className="spend">{text}</div>;
}
