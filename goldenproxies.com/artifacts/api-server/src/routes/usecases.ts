import { Router } from "express";

const router = Router();

const useCases = [
  {
    id: "youtube",
    title: "YouTube Automation",
    platform: "YouTube",
    description: "Manage multiple YouTube channels, view content from different regions, bypass geo-restrictions, and run automation scripts safely with residential IPs that YouTube cannot detect.",
    icon: "Youtube",
    category: "social",
    features: ["Multi-channel management", "Geo-restriction bypass", "View boosting", "Analytics automation"],
  },
  {
    id: "instagram",
    title: "Instagram Marketing",
    platform: "Instagram",
    description: "Manage multiple Instagram accounts, run automation tools for likes, follows, and DMs, and conduct social media marketing at scale with undetectable residential IPs.",
    icon: "Instagram",
    category: "social",
    features: ["Multi-account management", "DM automation", "Follow/unfollow automation", "Story viewing"],
  },
  {
    id: "tiktok",
    title: "TikTok Growth",
    platform: "TikTok",
    description: "Access TikTok from 200+ countries, manage multiple creator accounts, run engagement campaigns, and view region-locked content with IPs from real residential users.",
    icon: "Music",
    category: "social",
    features: ["Multi-account management", "Content access globally", "Engagement automation", "Regional trend analysis"],
  },
  {
    id: "spotify",
    title: "Spotify Streaming",
    platform: "Spotify",
    description: "Access Spotify from any country, stream music without geo-restrictions, manage multiple artist accounts, and conduct playlist marketing campaigns.",
    icon: "Headphones",
    category: "streaming",
    features: ["Geo-restriction bypass", "Stream count boosting", "Playlist promotion", "Artist account management"],
  },
  {
    id: "facebook",
    title: "Facebook Ads & Marketing",
    platform: "Facebook",
    description: "Run multiple Facebook ad accounts, manage business pages at scale, conduct competitor research, and run social media campaigns without account bans.",
    icon: "Facebook",
    category: "social",
    features: ["Multi-account ads", "Page management", "Competitor analysis", "Audience research"],
  },
  {
    id: "twitter",
    title: "Twitter / X Automation",
    platform: "Twitter",
    description: "Manage multiple Twitter/X accounts, run engagement campaigns, conduct social listening at scale, and automate content distribution across regions.",
    icon: "Twitter",
    category: "social",
    features: ["Multi-account management", "Tweet scheduling", "Engagement automation", "Social listening"],
  },
  {
    id: "scraping",
    title: "Web Scraping",
    platform: "Web Scraping",
    description: "Extract data from any website at scale without being blocked. Our rotating residential proxies ensure 99.5% success rates for e-commerce, real estate, and market research scrapers.",
    icon: "Database",
    category: "business",
    features: ["Rotating IP pools", "Anti-bot bypass", "High success rates", "CAPTCHA handling"],
  },
  {
    id: "seo",
    title: "SEO & SERP Monitoring",
    platform: "SEO Research",
    description: "Track search rankings from any location, monitor competitors, scrape SERPs at scale, and conduct local SEO research with location-specific residential proxies.",
    icon: "Search",
    category: "research",
    features: ["Geo-targeted searches", "SERP scraping", "Rank tracking", "Competitor monitoring"],
  },
];

router.get("/usecases", (req, res) => {
  res.json(useCases);
});

export default router;
