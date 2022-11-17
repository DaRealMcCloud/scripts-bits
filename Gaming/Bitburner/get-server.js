/** @param {NS} ns */
export async function main(ns) {
	var scriptName = ns.args[0];

	// How much RAM each purchased server will have. 

	var money = ns.getServerMoneyAvailable("home");
	for (let exp = 20; exp > 15; exp--) {
		var ram = Math.pow(2, exp);

		if (money > ns.getPurchasedServerCost(ram)) {
			break;
		}
	}


	// Iterator we'll use for our loop
	var purchasedServers = ns.getPurchasedServers();
	var i = purchasedServers.length

	// ns.tprint(purchasedServers);

	// Continuously try to purchase servers until we've reached the maximum
	// amount of servers
	while (i < ns.getPurchasedServerLimit()) {
		// Check if we have enough money to purchase a server
		money = ns.getServerMoneyAvailable("home");
		if (money > ns.getPurchasedServerCost(ram)) {
			// If we have enough money, then:
			//  1. Purchase the server
			//  2. Copy our hacking script onto the newly-purchased server
			//  3. Run our hacking script on the newly-purchased server with 3 threads
			//  4. Increment our iterator to indicate that we've bought a new server
			// Number of threads

			var hostname = ns.purchaseServer("pserv", ram);
			ns.tprint("Got server");
			ns.tprint(hostname);
			await ns.scp("multi-threader.js", hostname);
			await ns.scp("weaken.js", hostname);
			await ns.scp("grow.js", hostname);
			await ns.scp("hack.js", hostname);

			ns.run("starter.js", 1, hostname);
			++i;

			// only buy one instance
			ns.exit();
		}
		else {
			ns.printf("Not enough money.")
			if (ram > 1024) {
				ram = ram - 1024
			}
			else {
				await ns.sleep(10000);
			}
		}
	}
	ns.tprint("Got max servers.")
}