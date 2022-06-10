/** @param {NS} ns */
export async function main(ns) {
	var targets = ["n00dles", "nectar-net", "phantasy", "computek", "summit-uni", "syscore", "millenium-fitness", "foodnstuff", "sigma-cosmetics", "CSEC", "neo-net", "netlink", "joesguns", "zer0", "silver-helix", "hong-fang-tea", "harakiri-sushi", "max-hardware", "omega-net", "the-hub", "zb-institute", "alpha-ent", "snap-fitness", "unitalife", "univ-energy", "zb-def", "solaris", "aevum-police", "aerocorp", "deltaone", "zeus-med", "infocomm", "global-pharm", "I.I.I.I", "lexo-corp", "rho-construction", "galactic-cyber", "omnia", "defcomm", "icarus", "taiyang-digital", "nova-med", "johnson-ortho", "crush-fitness", "catalyst", "avmnite-02h", "rothman-uni"];
	//var scriptName = "pawn-all.js"
	var level = ns.getHackingLevel();

	for (let index = 0; index < targets.length; ++index) {
		const target = targets[index];
		var serverLevel = ns.getServerRequiredHackingLevel(target);

		if (level >= serverLevel) {
			//ns.tprint("Level sufficent.")
			if (ns.hasRootAccess(target)) {
				ns.run("master-hacker.js", 1, target)
			}
		}
	}
}