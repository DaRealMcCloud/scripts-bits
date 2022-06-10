/** @param {NS} ns */
export async function main(ns) {
	var script = ns.args[0];
	var level = ns.getHackingLevel();
	var ownServers = ns.getPurchasedServers()
	var initScan = ns.scan("home");
	var targets = [];

	//ns.tprint(initScan)
	for (let index = 0; index < initScan.length; index++) {
		var add = true
		if (initScan[index] == "home") {
			add = false
		} else {
			for (let ownServerIndex = 0; ownServerIndex < ownServers.length; ownServerIndex++) {
				if (initScan[index] == ownServers[ownServerIndex]) {
					add = false;
					//break;
				}
			}
		}
		if (add == true) {
			targets.push(initScan[index])
			ns.print("Run on ", initScan[index]);
			ns.run(script, 1, initScan[index])
		}
	}

	//ns.tprint(targets)
	while (true) {
		for (let index = 0; index < targets.length; index++) {
			// scan each server in the initial list.
			var scanResult = ns.scan(targets[index]);
			for (let index2 = 0; index2 < scanResult.length; index2++) {
				var serverLevel = ns.getServerRequiredHackingLevel(scanResult[index2]);
				//do not add home to targets
				var newTarget = true;
				if (scanResult[index2] == "home") {
					newTarget = false;
				} else if (level < serverLevel) {
					newTarget = false;
				} else if (ns.hasRootAccess(scanResult[index2]) == false) {
					newTarget = false;
				} else {
					for (let index3 = 0; index3 < targets.length; index3++) {
						if (scanResult[index2] == targets[index3]) {
							newTarget = false;
						}
					}
				}

				if (newTarget == true) {
					targets.push(scanResult[index2]);
					ns.print("Run on ", scanResult[index2]);
					ns.run(script, 1, scanResult[index2])
				}
			}
		}
		await ns.sleep(10000);
	}
}