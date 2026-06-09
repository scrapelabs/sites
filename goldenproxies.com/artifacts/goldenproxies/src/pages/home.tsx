import React from "react";
import { Link } from "wouter";
import { useGetProxyStats } from "@workspace/api-client-react";
import { Sparkles } from "@/components/sparkles";
import { Skeleton } from "@/components/ui/skeleton";
import { Globe, Server, Activity, ShieldCheck } from "lucide-react";

export default function Home() {
  const { data: stats, isLoading } = useGetProxyStats();

  return (
    <div className="w-full relative">
      {/* HERO SECTION */}
      <section className="relative pt-32 pb-24 overflow-hidden bg-background">
        <Sparkles />
        
        {/* Subtle background glow */}
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[800px] h-[800px] bg-primary/5 rounded-full blur-[100px] pointer-events-none"></div>
        
        <div className="container mx-auto px-4 relative z-10">
          <div className="max-w-4xl mx-auto text-center">
            <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-primary/5 border border-primary/20 mb-8 animate-fade-in-up" style={{ animationDelay: "0.1s" }}>
              <span className="w-2 h-2 rounded-full bg-primary animate-pulse"></span>
              <span className="text-sm font-semibold text-primary uppercase tracking-wider">Premium Proxy Network</span>
            </div>
            
            <h1 className="text-5xl md:text-7xl font-bold font-serif mb-6 leading-tight animate-fade-in-up" style={{ animationDelay: "0.2s" }}>
              The Gold Standard in <br className="hidden md:block"/>
              <span className="gold-gradient-text">Data Extraction</span>
            </h1>
            
            <p className="text-lg md:text-xl text-muted-foreground mb-10 max-w-2xl mx-auto animate-fade-in-up" style={{ animationDelay: "0.3s" }}>
              Elite residential and datacenter proxies built for professionals. Unmatched uptime, zero blocks, and absolute exclusivity.
            </p>
            
            <div className="flex flex-col sm:flex-row items-center justify-center gap-4 animate-fade-in-up" style={{ animationDelay: "0.4s" }}>
              <Link href="/plans" className="w-full sm:w-auto px-8 py-4 rounded-full text-base font-bold gold-button text-center">
                Explore Premium Plans
              </Link>
              <Link href="/contact" className="w-full sm:w-auto px-8 py-4 rounded-full text-base font-bold bg-white text-foreground border border-primary/20 hover:border-primary/50 transition-all shadow-sm hover:shadow-md text-center">
                Contact Sales
              </Link>
            </div>
          </div>
        </div>
      </section>

      {/* STATS SECTION */}
      <section className="py-20 relative bg-primary/[0.02] border-y border-primary/10">
        <div className="container mx-auto px-4">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <StatCard 
              icon={<Globe className="w-6 h-6 text-primary" />}
              label="Global IPs"
              value={isLoading ? <Skeleton className="h-10 w-24 mx-auto" /> : stats?.totalIPs}
            />
            <StatCard 
              icon={<Server className="w-6 h-6 text-primary" />}
              label="Countries"
              value={isLoading ? <Skeleton className="h-10 w-16 mx-auto" /> : stats?.countries}
            />
            <StatCard 
              icon={<Activity className="w-6 h-6 text-primary" />}
              label="Uptime"
              value={isLoading ? <Skeleton className="h-10 w-20 mx-auto" /> : stats?.uptime}
            />
            <StatCard 
              icon={<ShieldCheck className="w-6 h-6 text-primary" />}
              label="Success Rate"
              value={isLoading ? <Skeleton className="h-10 w-20 mx-auto" /> : stats?.successRate}
            />
          </div>
        </div>
      </section>

      {/* FEATURES HIGHLIGHT */}
      <section className="py-24 bg-background">
        <div className="container mx-auto px-4">
          <div className="text-center max-w-2xl mx-auto mb-16">
            <h2 className="text-3xl md:text-4xl font-bold font-serif mb-4">Engineered for Excellence</h2>
            <p className="text-muted-foreground text-lg">Every aspect of our network is optimized to provide the most reliable, undetectable proxy experience possible.</p>
          </div>
          
          <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
            <FeatureCard 
              title="Pristine IP Pool" 
              description="Ethically sourced, continuously rotated residential IPs that bypass the most aggressive anti-bot systems."
            />
            <FeatureCard 
              title="Lightning Speed" 
              description="Dedicated backbone infrastructure ensures latency is measured in milliseconds, not seconds."
            />
            <FeatureCard 
              title="Absolute Privacy" 
              description="Zero logs, military-grade encryption, and completely anonymous browsing protocols."
            />
          </div>
        </div>
      </section>
    </div>
  );
}

function StatCard({ icon, label, value }: { icon: React.ReactNode, label: string, value: React.ReactNode }) {
  return (
    <div className="glass-card rounded-2xl p-8 text-center transition-transform hover:-translate-y-1 duration-300">
      <div className="w-12 h-12 mx-auto rounded-full bg-primary/10 flex items-center justify-center mb-4">
        {icon}
      </div>
      <div className="text-3xl md:text-4xl font-bold font-serif text-foreground mb-2">
        {value}
      </div>
      <div className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
        {label}
      </div>
    </div>
  );
}

function FeatureCard({ title, description }: { title: string, description: string }) {
  return (
    <div className="glass-card rounded-2xl p-8 relative overflow-hidden group">
      <div className="absolute top-0 right-0 w-32 h-32 bg-primary/5 rounded-full blur-2xl group-hover:bg-primary/10 transition-colors"></div>
      <h3 className="text-xl font-bold font-serif mb-4 text-foreground relative z-10">{title}</h3>
      <p className="text-muted-foreground leading-relaxed relative z-10">{description}</p>
      
      <div className="mt-8 flex items-center gap-2 text-primary font-semibold text-sm group-hover:gap-3 transition-all relative z-10">
        Learn more <span className="text-lg leading-none">&rarr;</span>
      </div>
    </div>
  );
}
