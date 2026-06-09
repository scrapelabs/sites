import React from "react";
import { Link } from "wouter";
import { useHealthCheck } from "@workspace/api-client-react";

export function Footer() {
  const { data: health, isLoading, isError } = useHealthCheck();
  
  const statusText = isLoading ? "Checking Status..." : isError ? "Service Degraded" : health?.status === "ok" ? "Operational" : "Unknown Status";
  const statusColor = isLoading ? "bg-yellow-500 shadow-[0_0_8px_rgba(234,179,8,0.6)]" : isError ? "bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.6)]" : health?.status === "ok" ? "bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]" : "bg-gray-500";

  return (
    <footer className="bg-white border-t border-primary/10 pt-20 pb-10 mt-auto">
      <div className="container mx-auto px-4">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-12 mb-16">
          <div className="col-span-1 md:col-span-2">
            <Link href="/" className="flex items-center gap-2 mb-6">
              <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-primary to-primary/60 flex items-center justify-center">
                <div className="w-3 h-3 bg-white rotate-45 transform"></div>
              </div>
              <span className="font-serif font-bold text-2xl gold-gradient-text tracking-tight">GoldenProxies</span>
            </Link>
            <p className="text-muted-foreground max-w-sm mb-6">
              The world's most exclusive proxy network. Built for performance, designed for professionals. Unmatched speed and reliability.
            </p>
          </div>
          
          <div>
            <h4 className="font-serif font-bold text-lg mb-6 text-foreground">Services</h4>
            <ul className="space-y-4">
              <li><Link href="/plans" className="text-muted-foreground hover:text-primary transition-colors">Residential Proxies</Link></li>
              <li><Link href="/plans" className="text-muted-foreground hover:text-primary transition-colors">Datacenter Proxies</Link></li>
              <li><Link href="/plans" className="text-muted-foreground hover:text-primary transition-colors">Mobile Proxies</Link></li>
              <li><Link href="/use-cases" className="text-muted-foreground hover:text-primary transition-colors">Use Cases</Link></li>
            </ul>
          </div>
          
          <div>
            <h4 className="font-serif font-bold text-lg mb-6 text-foreground">Company</h4>
            <ul className="space-y-4">
              <li><Link href="/contact" className="text-muted-foreground hover:text-primary transition-colors">Contact Us</Link></li>
              <li><a href="#" className="text-muted-foreground hover:text-primary transition-colors">Terms of Service</a></li>
              <li><a href="#" className="text-muted-foreground hover:text-primary transition-colors">Privacy Policy</a></li>
              <li><a href="#" className="text-muted-foreground hover:text-primary transition-colors">API Documentation</a></li>
            </ul>
          </div>
        </div>
        
        <div className="pt-8 border-t border-primary/10 flex flex-col md:flex-row items-center justify-between gap-4">
          <p className="text-sm text-muted-foreground">
            &copy; {new Date().getFullYear()} GoldenProxies. All rights reserved.
          </p>
          <div className="flex items-center gap-4 bg-background px-4 py-2 rounded-full border border-border/50">
            <div className={`w-2.5 h-2.5 rounded-full ${statusColor}`}></div>
            <span className="text-xs font-semibold text-foreground/80 tracking-wide uppercase">Network: {statusText}</span>
          </div>
        </div>
      </div>
    </footer>
  );
}
