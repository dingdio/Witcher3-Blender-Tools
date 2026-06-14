using UnrealBuildTool;

public class WitcherToolsImporter : ModuleRules
{
    public WitcherToolsImporter(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

        PublicDependencyModuleNames.AddRange(new[]
        {
            "Core",
            "CoreUObject",
            "Engine",
            "Json",
            "JsonUtilities",
            "Sockets",
            "Networking"
        });

        PrivateDependencyModuleNames.AddRange(new[]
        {
            "AssetRegistry",
            "AssetTools",
            "MaterialEditor",
            "UnrealEd",
            "WitcherToolsRuntime"
        });
    }
}
