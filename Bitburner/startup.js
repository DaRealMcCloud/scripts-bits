/** @param {NS} ns */
export async function main(ns) {

	ns.run("stockbot.js",1);
	ns.run("get-server.js",1);


	if(ns.isRunning("hacknetCash.js","home")) {
		// hackNet manager already running.
	} else {
		ns.run("hacknetCash.js",1);
	}

	
}