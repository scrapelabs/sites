import React from "react";
import { useListUseCases } from "@workspace/api-client-react";
import { Skeleton } from "@/components/ui/skeleton";
import { SiYoutube, SiInstagram, SiTiktok, SiSpotify, SiFacebook, SiX } from "react-icons/si";
import { Database, Search } from "lucide-react";

const IconMap: Record<string, React.ReactNode> = {
  youtube: <SiYoutube className="w-8 h-8" />,
  instagram: <SiInstagram className="w-8 h-8" />,
  tiktok: <SiTiktok className="w-8 h-8" />,
  spotify: <SiSpotify className="w-8 h-8" />,
  facebook: <SiFacebook className="w-8 h-8" />,
  twitter: <SiX className="w-8 h-8" />,
  scraping: <Database className="w-8 h-8" />,
  seo: <Search className="w-8 h-8" />,
};

export default function UseCases() {
  const { data: useCases, isLoading } = useListUseCases();

  return (
    <div className="w-full pt-16 pb-24 bg-background">
      <div className="container mx-auto px-4">
        <div className="text-center max-w-3xl mx-auto mb-16">
          <h1 className="text-4xl md:text-5xl font-bold font-serif mb-6">
            Empower Your <span className="gold-gradient-text">Operations</span>
          </h1>
          <p className="text-lg text-muted-foreground">
            From social media management to aggressive data scraping, our infrastructure supports the most demanding digital workflows.
          </p>
        </div>

        {isLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 max-w-6xl mx-auto">
            {[1, 2, 3, 4, 5, 6].map(i => (
              <div key={i} className="glass-card rounded-2xl p-6">
                <Skeleton className="w-12 h-12 rounded-full mb-4" />
                <Skeleton className="h-6 w-1/2 mb-2" />
                <Skeleton className="h-4 w-full mb-1" />
                <Skeleton className="h-4 w-3/4 mb-6" />
                <div className="space-y-2">
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-5/6" />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 max-w-6xl mx-auto">
            {useCases?.map((useCase) => (
              <div key={useCase.id} className="glass-card rounded-2xl p-8 hover:shadow-lg transition-all duration-300 group">
                <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-primary/10 to-primary/5 flex items-center justify-center text-primary mb-6 group-hover:scale-110 transition-transform shadow-sm border border-primary/20">
                  {IconMap[useCase.icon.toLowerCase()] || <div className="w-8 h-8 bg-primary/20 rounded-full" />}
                </div>
                
                <div className="text-xs font-bold uppercase tracking-wider text-primary mb-2">
                  {useCase.category}
                </div>
                
                <h3 className="text-xl font-bold font-serif text-foreground mb-3">{useCase.title}</h3>
                
                <p className="text-muted-foreground text-sm leading-relaxed mb-6">
                  {useCase.description}
                </p>
                
                <ul className="space-y-2 border-t border-primary/10 pt-4">
                  {useCase.features.slice(0, 3).map((feature: string, idx: number) => (
                    <li key={idx} className="flex items-center gap-2 text-sm text-foreground/80 font-medium">
                      <div className="w-1.5 h-1.5 rounded-full bg-primary" />
                      {feature}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
