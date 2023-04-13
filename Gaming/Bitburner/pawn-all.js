/** @param {NS} ns */
export async function main(ns) {
	ns.disableLog("ALL")
	//var targets = ["n00dles", "nectar-net", "phantasy", "computek", "summit-uni", "syscore", "millenium-fitness", "foodnstuff", "sigma-cosmetics", "CSEC", "neo-net", "netlink", "joesguns", "zer0", "silver-helix", "hong-fang-tea", "harakiri-sushi", "max-hardware", "omega-net", "the-hub", "zb-institute", "alpha-ent", "snap-fitness", "unitalife", "univ-energy", "zb-def", "solaris", "aevum-police", "aerocorp", "deltaone", "zeus-med", "infocomm", "global-pharm", "I.I.I.I", "lexo-corp", "rho-construction", "galactic-cyber", "omnia", "defcomm", "icarus", "taiyang-digital", "nova-med", "johnson-ortho", "crush-fitness", "catalyst", "avmnite-02h", "rothman-uni"];
	//var scriptName = "pawn-all.js"
	var level = ns.getHackingLevel();
	var targets = ns.scan();

	for (let index = 0; index < targets.length; index++) {
		// scan each server in the initial list.
		var scanResult = ns.scan(targets[index]);
		for (let index2 = 0; index2 < scanResult.length; index2++) {
			//do not add home to targets
			if (scanResult[index2] != "home") {
				var newTarget = true;
				// check if the list of targets contains the new reult already
				for (let index3 = 0; index3 < targets.length; index3++) {
					if (scanResult[index2] == targets[index3]) {
						newTarget = false;
					}
				}
				// add to targets if the entry does not exist in the targets
				if (newTarget == true) {
					targets.push(scanResult[index2]);
				}
			}
		}
	}

	ns.print(targets)

	while (true) {
		level = ns.getHackingLevel();
		for (let index = 0; index < targets.length; ++index) {
			var target = targets[index];
			var serverLevel = ns.getServerRequiredHackingLevel(target);

			if (level >= serverLevel) {
				//ns.tprint("Level sufficent.")
				if (ns.hasRootAccess(target)) {
					//ns.tprint("Already root.");
				} else {
					var attacks = 0
					if (ns.fileExists("relaySMTP.exe", "home")) {
						ns.relaysmtp(target);
						attacks++
					}
					if (ns.fileExists("BruteSSH.exe", "home")) {
						ns.brutessh(target);
						attacks++
					}
					if (ns.fileExists("FTPcrack.exe", "home")) {
						ns.ftpcrack(target);
						attacks++
					}
					if (ns.fileExists("HTTPWorm.exe", "home")) {
						ns.httpworm(target);
						attacks++
					}
					if (ns.fileExists("SQLInject.exe", "home")) {
						ns.sqlinject(target);
						attacks++
					}
					if (attacks >= ns.getServerNumPortsRequired(target)) {
						ns.getServerNumPortsRequired(target)
						ns.tprint("Pawning ", target)
						ns.nuke(target);
						//ns.exec("backdoor",target)
					}
				}
				//await ns.scp(scriptName, target)
				//ns.exec(scriptName, target, 1);
				//ns.exit()
			}
		}
		await ns.sleep(10000)
	}
}