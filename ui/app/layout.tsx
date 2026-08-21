import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { Spend } from "@/components/Spend";

export const metadata: Metadata = {
  title: "Cairn",
  description: "Agentic incident analysis",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <div className="inner">
            <h1>Cairn</h1>
            <nav>
              <Link href="/">Ask</Link>
              <Link href="/approvals">Approvals</Link>
            </nav>
            <Spend />
          </div>
        </header>
        <main className="shell">{children}</main>
      </body>
    </html>
  );
}
