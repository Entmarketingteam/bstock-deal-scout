-- Add AI mockup URL storage to listings
-- Stores JSONB: {"brushed_nickel": "https://...", "matte_black": "https://..."}
ALTER TABLE bstock_listings
  ADD COLUMN IF NOT EXISTS ai_mockup_url JSONB DEFAULT NULL;
