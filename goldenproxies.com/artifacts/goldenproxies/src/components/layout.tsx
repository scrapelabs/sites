import React from "react";
import { NavBar } from "@/components/nav-bar";
import { Footer } from "@/components/footer";
import { SupportWidget } from "@/components/support-widget";

interface LayoutProps {
  children: React.ReactNode;
}

export function Layout({ children }: LayoutProps) {
  return (
    <div className="min-h-[100dvh] flex flex-col bg-background text-foreground font-sans relative selection:bg-primary/20 selection:text-primary">
      <NavBar />
      <main className="flex-1 pt-20">
        {children}
      </main>
      <Footer />
      <SupportWidget />
    </div>
  );
}
