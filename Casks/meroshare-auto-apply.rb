cask "meroshare-auto-apply" do
  version "2026.05.07.4"
  sha256 "89d3464f30331e4cf450b516536fad112fa9f8c699f1c420fce2c01c30a4dab7"

  url "https://github.com/OfficialBishal/MeroShare-Auto-Apply/releases/download/v#{version}/MeroShare-Auto-Apply.dmg"
  name "MeroShare Auto-Apply"
  desc "Menu bar tool that auto-applies to NEPSE IPOs and rights via MeroShare"
  homepage "https://github.com/OfficialBishal/MeroShare-Auto-Apply"

  livecheck do
    url :url
    strategy :github_latest
  end

  auto_updates true
  depends_on macos: ">= :catalina"

  app "MeroShare Auto-Apply.app"

  zap trash: [
    "~/Library/Application Support/MeroShare Auto-Apply",
    "~/Library/Logs/MeroShare Auto-Apply",
    "~/Library/Preferences/com.meroshare.autoapply.plist",
    "~/Library/Saved Application State/com.meroshare.autoapply.savedState",
  ]
end
