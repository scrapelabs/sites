import React, { useState } from "react";
import { useListProxyPlans } from "@workspace/api-client-react";
import { Skeleton } from "@/components/ui/skeleton";
import { Check } from "lucide-react";
import { Link } from "wouter";

const PROXY_TYPES = [
  {
    key: "residential",
    label: "Residential Proxies",
    tagline: "Real IPs from genuine ISP households — undetectable at scale.",
    badge: "Most Popular",
  },
  {
    key: "ipv6",
    label: "IPv6 Proxies",
    tagline: "Millions of fresh IPv6 addresses at lightning-fast speeds.",
    badge: null,
  },
  {
    key: "datacenter",
    label: "Datacenter Proxies",
    tagline: "Blazing-fast dedicated datacenter IPs with 99.9% uptime SLA.",
    badge: null,
  },
];

const TYPE_LABELS: Record<string, string> = {
  residential: "Residential",
  ipv6: "IPv6",
  datacenter: "Datacenter",
};

export default function Plans() {
  const { data: plans, isLoading } = useListProxyPlans();
  const [activeType, setActiveType] = useState("residential");

  const filtered = plans?.filter((p: any) => p.type === activeType) ?? [];
  const activeTypeMeta = PROXY_TYPES.find((t) => t.key === activeType)!;

  return (
    <div className="w-full pt-16 pb-24 bg-background">
      <div className="container mx-auto px-4">

        {/* Page header */}
        <div className="text-center max-w-3xl mx-auto mb-12">
          <h1 className="text-4xl md:text-5xl font-bold font-serif mb-6">
            Invest in <span className="gold-gradient-text">Performance</span>
          </h1>
          <p className="text-lg text-muted-foreground">
            Three premium proxy types. Every tier built for professionals who demand results.
          </p>
        </div>

        {/* Proxy type tabs */}
        <div className="flex flex-wrap justify-center gap-3 mb-12">
          {PROXY_TYPES.map((t) => (
            <button
              key={t.key}
              onClick={() => setActiveType(t.key)}
              className={`relative px-6 py-3 rounded-full text-sm font-semibold transition-all duration-200 border ${
                activeType === t.key
                  ? "gold-button text-white shadow-lg shadow-primary/25 border-transparent"
                  : "bg-white border-primary/20 text-foreground hover:border-primary hover:bg-primary/5"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Active type tagline */}
        <div className="text-center mb-10">
          <p className="text-muted-foreground text-base italic">
            {activeTypeMeta.tagline}
          </p>
        </div>

        {/* Plans grid */}
        {isLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-8 max-w-6xl mx-auto">
            {[1, 2, 3].map((i) => (
              <div key={i} className="glass-card rounded-3xl p-8 h-[520px]">
                <Skeleton className="h-6 w-1/3 mb-2" />
                <Skeleton className="h-10 w-1/2 mb-8" />
                <div className="space-y-4">
                  {[1, 2, 3, 4, 5].map((j) => (
                    <Skeleton key={j} className="h-4 w-full" />
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className={`grid grid-cols-1 gap-8 max-w-6xl mx-auto ${
            filtered.length === 4
              ? "md:grid-cols-2 lg:grid-cols-4"
              : filtered.length === 3
              ? "md:grid-cols-3"
              : "md:grid-cols-2 lg:grid-cols-3"
          }`}>
            {filtered.map((plan: any) => (
              <PlanCard key={plan.id} plan={plan} />
            ))}
          </div>
        )}

        {/* Compare note */}
        <p className="text-center text-sm text-muted-foreground mt-12">
          Not sure which type fits your needs?{" "}
          <Link href="/contact" className="text-primary font-semibold hover:underline">
            Talk to our team
          </Link>{" "}
          — we'll match you with the right solution.
        </p>
      </div>
    </div>
  );
}

function PlanCard({ plan }: { plan: any }) {
  const isPopular = plan.popular;

  return (
    <div
      className={`glass-card rounded-3xl p-7 relative flex flex-col transition-all duration-300 hover:shadow-xl hover:-translate-y-1 ${
        isPopular ? "border-primary ring-1 ring-primary/30 md:-translate-y-3" : ""
      }`}
    >
      {isPopular && (
        <div className="absolute top-0 left-1/2 -translate-x-1/2 -translate-y-1/2 gold-button text-white px-4 py-1 rounded-full text-xs font-bold uppercase tracking-wider shadow-md shadow-primary/20 whitespace-nowrap">
          Most Popular
        </div>
      )}

      {/* Header */}
      <div className="mb-6 pt-2">
        <span className="inline-block text-xs font-bold uppercase tracking-widest text-primary mb-2">
          {TYPE_LABELS[plan.type]}
        </span>
        <h3 className="text-2xl font-bold font-serif text-foreground mb-1">{plan.name}</h3>
        <div className="flex items-baseline gap-1 mt-4">
          <span className="text-4xl font-bold text-foreground">${plan.price}</span>
          <span className="text-muted-foreground font-medium text-sm">/ {plan.bandwidth}</span>
        </div>
      </div>

      {/* Divider */}
      <div className="h-px bg-gradient-to-r from-transparent via-primary/20 to-transparent mb-6" />

      {/* Features */}
      <div className="flex-1 mb-8">
        <ul className="space-y-3">
          <li className="flex items-start gap-3">
            <Check className="w-4 h-4 text-primary shrink-0 mt-0.5" />
            <span className="text-foreground text-sm">{plan.locations} Global Locations</span>
          </li>
          {plan.features.map((feature: string, idx: number) => (
            <li key={idx} className="flex items-start gap-3">
              <Check className="w-4 h-4 text-primary shrink-0 mt-0.5" />
              <span className="text-foreground text-sm">{feature}</span>
            </li>
          ))}
        </ul>
      </div>

      {/* CTA */}
      <Link
        href={`/contact?plan=${plan.id}`}
        className={`w-full py-3.5 rounded-xl text-center text-sm font-bold transition-all block ${
          isPopular
            ? "gold-button text-white"
            : "bg-white border-2 border-primary/20 text-foreground hover:border-primary hover:bg-primary/5"
        }`}
      >
        Get Started
      </Link>
    </div>
  );
}
