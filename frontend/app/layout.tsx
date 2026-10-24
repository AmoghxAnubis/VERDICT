import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "VERDICT · Oracle intelligence",
  description: "Trace every oracle decision to its sources, weights, and retained evidence.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
