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
            "WitcherToolsRuntime",
            "Foliage",
            "Landscape",
            "LandscapeEditor",
            "MeshDescription",
            "StaticMeshDescription",
            "SkeletalMeshDescription",
            "SkeletalMeshUtilitiesCommon",
            "AnimationCore",
            "Slate",
            "SlateCore",
            "ToolMenus",
            "LevelEditor",
            "EditorFramework",
            "IKRig",
            "IKRigDeveloper",
            "IKRigEditor",
            "ControlRig",
            "ControlRigDeveloper",
            "ControlRigEditor",
            "ContentBrowser",
            "RigVM",
            "RigVMDeveloper",
            "RigVMEditor",
            "FullBodyIK"
        });
    }
}
