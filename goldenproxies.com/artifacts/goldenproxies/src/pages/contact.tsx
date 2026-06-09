import React, { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useSearch } from "wouter";
import { useSubmitLead, useGetProxyPlan, getGetProxyPlanQueryKey } from "@workspace/api-client-react";
import { useToast } from "@/hooks/use-toast";
import { 
  Form, 
  FormControl, 
  FormField, 
  FormItem, 
  FormLabel, 
  FormMessage 
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const leadSchema = z.object({
  name: z.string().min(2, "Name is required"),
  email: z.string().email("Invalid email address"),
  company: z.string().optional(),
  message: z.string().min(10, "Please tell us about your needs"),
});

type LeadFormValues = z.infer<typeof leadSchema>;

export default function Contact() {
  const { toast } = useToast();
  const submitLead = useSubmitLead();
  const searchString = useSearch();
  const searchParams = new URLSearchParams(searchString);
  const planId = searchParams.get("plan");

  const { data: planData } = useGetProxyPlan(planId || "", { 
    query: { 
      enabled: !!planId,
      queryKey: getGetProxyPlanQueryKey(planId || "") 
    } 
  });
  
  const form = useForm<LeadFormValues>({
    resolver: zodResolver(leadSchema),
    defaultValues: {
      name: "",
      email: "",
      company: "",
      message: "",
    },
  });

  useEffect(() => {
    if (planData && !form.getValues().message) {
      form.setValue("message", `I am interested in the ${planData.name} (${planData.type}) plan for $${planData.price}/${planData.bandwidth}. Please provide more information about how to get started with this tier.`);
    }
  }, [planData, form]);

  const onSubmit = (data: LeadFormValues) => {
    submitLead.mutate(
      { data },
      {
        onSuccess: () => {
          toast({
            title: "Request Received",
            description: "A dedicated account manager will contact you shortly.",
            className: "bg-white border-primary text-foreground",
          });
          form.reset();
        },
        onError: () => {
          toast({
            title: "Submission Failed",
            description: "Please try again later or email us directly.",
            variant: "destructive",
          });
        }
      }
    );
  };

  return (
    <div className="w-full pt-16 pb-24 bg-background relative overflow-hidden">
      {/* Decorative background elements */}
      <div className="absolute top-0 right-0 w-1/3 h-full bg-primary/5 rounded-l-full blur-3xl pointer-events-none transform translate-x-1/2"></div>
      
      <div className="container mx-auto px-4 relative z-10">
        <div className="max-w-5xl mx-auto grid grid-cols-1 lg:grid-cols-2 gap-16">
          
          <div className="pt-8">
            <h1 className="text-4xl md:text-5xl font-bold font-serif mb-6">
              Request <span className="gold-gradient-text">Access</span>
            </h1>
            <p className="text-lg text-muted-foreground mb-12">
              Our network is strictly curated to ensure the highest performance for our clients. Contact our sales team to discuss your bespoke infrastructure needs.
            </p>
            
            <div className="space-y-8">
              <div className="flex gap-4">
                <div className="w-12 h-12 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-primary"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg>
                </div>
                <div>
                  <h4 className="font-bold text-foreground">Direct Line</h4>
                  <p className="text-muted-foreground">+1 (800) 555-GOLD</p>
                </div>
              </div>
              
              <div className="flex gap-4">
                <div className="w-12 h-12 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-primary"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg>
                </div>
                <div>
                  <h4 className="font-bold text-foreground">Priority Email</h4>
                  <p className="text-muted-foreground">concierge@goldenproxies.com</p>
                </div>
              </div>
            </div>
          </div>
          
          <div className="glass-card rounded-3xl p-8 md:p-10 shadow-xl relative">
            {planData && (
              <div className="absolute -top-4 right-8 bg-primary text-white text-xs font-bold px-4 py-1 rounded-full shadow-lg">
                Selected: {planData.name} Plan
              </div>
            )}
            <h3 className="text-2xl font-bold font-serif mb-8">Send a Message</h3>
            
            <Form {...form}>
              <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
                <FormField
                  control={form.control}
                  name="name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-foreground font-semibold">Full Name</FormLabel>
                      <FormControl>
                        <Input placeholder="John Doe" {...field} className="bg-white/50 border-primary/20 focus:border-primary" />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                
                <FormField
                  control={form.control}
                  name="email"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-foreground font-semibold">Corporate Email</FormLabel>
                      <FormControl>
                        <Input placeholder="john@company.com" {...field} className="bg-white/50 border-primary/20 focus:border-primary" />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                
                <FormField
                  control={form.control}
                  name="company"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-foreground font-semibold">Company (Optional)</FormLabel>
                      <FormControl>
                        <Input placeholder="Acme Corp" {...field} className="bg-white/50 border-primary/20 focus:border-primary" />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                
                <FormField
                  control={form.control}
                  name="message"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel className="text-foreground font-semibold">Requirements</FormLabel>
                      <FormControl>
                        <Textarea 
                          placeholder="Tell us about your proxy needs, target platforms, and expected volume..." 
                          className="min-h-[120px] bg-white/50 border-primary/20 focus:border-primary resize-none"
                          {...field} 
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                
                <button 
                  type="submit" 
                  disabled={submitLead.isPending}
                  className="w-full py-4 rounded-xl text-base font-bold gold-button mt-4 disabled:opacity-70 disabled:cursor-not-allowed"
                >
                  {submitLead.isPending ? "Submitting Request..." : "Request Access"}
                </button>
              </form>
            </Form>
          </div>
          
        </div>
      </div>
    </div>
  );
}
