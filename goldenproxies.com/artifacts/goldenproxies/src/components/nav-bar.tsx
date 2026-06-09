import React, { useState } from "react";
import { Link, useLocation } from "wouter";
import { Show, SignInButton, SignUpButton, useUser, useClerk } from "@clerk/react";
import { User, LogOut, LayoutDashboard, ChevronDown } from "lucide-react";

export function NavBar() {
  const [location] = useLocation();
  const { user, isSignedIn } = useUser();
  const { signOut } = useClerk();
  const [dropdownOpen, setDropdownOpen] = useState(false);

  return (
    <header className="fixed top-0 left-0 right-0 z-50 transition-all duration-300 backdrop-blur-md bg-white/70 border-b border-primary/10">
      <div className="container mx-auto px-4 h-20 flex items-center justify-between">
        <Link href="/" className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-primary to-primary/60 flex items-center justify-center shadow-md shadow-primary/20">
            <div className="w-3 h-3 bg-white rotate-45 transform"></div>
          </div>
          <span className="font-serif font-bold text-2xl gold-gradient-text tracking-tight">GoldenProxies</span>
        </Link>

        {/* Public nav — only visible when signed out */}
        {!isSignedIn && (
          <nav className="hidden md:flex items-center gap-8">
            <Link href="/plans" className={`text-sm font-medium transition-colors hover:text-primary ${location === "/plans" ? "text-primary" : "text-foreground/80"}`}>
              Pricing Plans
            </Link>
            <Link href="/use-cases" className={`text-sm font-medium transition-colors hover:text-primary ${location === "/use-cases" ? "text-primary" : "text-foreground/80"}`}>
              Use Cases
            </Link>
            <Link href="/contact" className={`text-sm font-medium transition-colors hover:text-primary ${location === "/contact" ? "text-primary" : "text-foreground/80"}`}>
              Contact
            </Link>
          </nav>
        )}

        <div className="flex items-center gap-3">
          {/* Signed out */}
          <Show when="signed-out">
            <SignInButton mode="modal">
              <button className="hidden sm:inline-flex px-5 py-2 rounded-full text-sm font-semibold border border-primary/25 text-foreground hover:border-primary hover:bg-primary/5 transition-all">
                Sign in
              </button>
            </SignInButton>
            <SignUpButton mode="modal">
              <button className="px-5 py-2.5 rounded-full text-sm font-bold gold-button text-white">
                Get Started
              </button>
            </SignUpButton>
          </Show>

          {/* Signed in */}
          <Show when="signed-in">
            <Link href="/dashboard" className="hidden sm:inline-flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium border border-primary/20 text-foreground hover:border-primary hover:bg-primary/5 transition-all">
              <LayoutDashboard className="w-4 h-4 text-primary" />
              Dashboard
            </Link>
            <div className="relative">
              <button
                onClick={() => setDropdownOpen(!dropdownOpen)}
                onBlur={() => setTimeout(() => setDropdownOpen(false), 150)}
                className="flex items-center gap-2 px-3 py-2 rounded-full border border-primary/20 hover:border-primary hover:bg-primary/5 transition-all"
              >
                <div className="w-7 h-7 rounded-full bg-gradient-to-br from-primary to-primary/60 flex items-center justify-center">
                  <User className="w-4 h-4 text-white" />
                </div>
                <span className="hidden sm:block text-sm font-medium text-foreground max-w-[100px] truncate">
                  {user?.firstName || user?.emailAddresses[0]?.emailAddress?.split("@")[0]}
                </span>
                <ChevronDown className="w-3 h-3 text-muted-foreground" />
              </button>

              {dropdownOpen && (
                <div className="absolute right-0 top-full mt-2 w-52 bg-white border border-primary/15 rounded-2xl shadow-xl shadow-primary/10 overflow-hidden z-50">
                  <div className="px-4 py-3 border-b border-primary/10">
                    <div className="text-xs font-medium text-foreground truncate">
                      {user?.firstName || "My Account"}
                    </div>
                    <div className="text-xs text-muted-foreground truncate">
                      {user?.emailAddresses[0]?.emailAddress}
                    </div>
                  </div>
                  <div className="p-2">
                    <Link
                      href="/dashboard"
                      className="flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium text-foreground hover:bg-primary/5 transition-colors"
                    >
                      <LayoutDashboard className="w-4 h-4 text-primary" />
                      Dashboard
                    </Link>
                    <button
                      onClick={() => signOut()}
                      className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium text-foreground hover:bg-red-50 hover:text-red-600 transition-colors"
                    >
                      <LogOut className="w-4 h-4" />
                      Sign out
                    </button>
                  </div>
                </div>
              )}
            </div>
          </Show>
        </div>
      </div>
    </header>
  );
}
