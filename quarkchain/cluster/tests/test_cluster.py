import unittest
from quarkchain.genesis import GenesisManager
from quarkchain.cluster.tests.test_utils import (
    create_transfer_transaction,
    ClusterContext,
)
from quarkchain.core import Address, Branch, Identity
from quarkchain.evm import opcodes
from quarkchain.utils import call_async, assert_true_with_timeout


class TestCluster(unittest.TestCase):
    def test_single_cluster(self):
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)
        with ClusterContext(1, acc1) as clusters:
            self.assertEqual(len(clusters), 1)

    def test_three_clusters(self):
        with ClusterContext(3) as clusters:
            self.assertEqual(len(clusters), 3)

    def test_create_shard_at_different_height(self):
        acc1 = Address.create_random_account()
        id1 = 0 << 16 | 1 | 0
        id2 = 1 << 16 | 1 | 0
        genesis_root_heights = {id1: 1, id2: 2}
        with ClusterContext(
            1,
            acc1,
            chain_size=2,
            shard_size=1,
            genesis_root_heights=genesis_root_heights,
        ) as clusters:
            master = clusters[0].master

            self.assertIsNone(clusters[0].get_shard(id1))
            self.assertIsNone(clusters[0].get_shard(id2))

            is_root, root = call_async(master.get_next_block_to_mine(acc1))
            self.assertTrue(is_root)
            self.assertEqual(len(root.minor_block_header_list), 0)
            call_async(master.add_root_block(root))

            # shard 0 created at root height 1
            self.assertIsNotNone(clusters[0].get_shard(id1))
            self.assertIsNone(clusters[0].get_shard(id2))

            is_root, root = call_async(master.get_next_block_to_mine(acc1))
            self.assertTrue(is_root)
            self.assertEqual(len(root.minor_block_header_list), 1)
            call_async(master.add_root_block(root))

            self.assertIsNotNone(clusters[0].get_shard(id1))
            # shard 1 created at root height 2
            self.assertIsNotNone(clusters[0].get_shard(id2))

            is_root, block = call_async(master.get_next_block_to_mine(acc1))
            self.assertTrue(is_root)
            self.assertEqual(len(root.minor_block_header_list), 1)
            call_async(master.add_root_block(root))

    def test_get_next_block_to_mine(self):
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)
        acc2 = Address.create_random_account(full_shard_key=0)
        acc3 = Address.create_random_account(full_shard_key=1)
        acc4 = Address.create_random_account(full_shard_key=1)

        with ClusterContext(1, acc1) as clusters:
            master = clusters[0].master
            slaves = clusters[0].slave_list

            # Expect to mine root that includes the genesis minor blocks
            is_root, block = call_async(master.get_next_block_to_mine(address=acc2))
            self.assertTrue(is_root)
            self.assertEqual(block.header.height, 1)
            self.assertEqual(len(block.minor_block_header_list), 4)
            self.assertEqual(block.minor_block_header_list[0].height, 0)
            self.assertEqual(block.minor_block_header_list[1].height, 0)
            call_async(master.add_root_block(block))

            tx = create_transfer_transaction(
                shard_state=clusters[0].get_shard_state(0b10),
                key=id1.get_key(),
                from_address=acc1,
                to_address=acc3,
                value=54321,
                gas=opcodes.GTXXSHARDCOST + opcodes.GTXCOST,
                gas_price=3,
            )
            self.assertTrue(slaves[0].add_tx(tx))

            # Expect to mine shard 0 since it has one tx
            is_root, block1 = call_async(master.get_next_block_to_mine(address=acc2))
            self.assertFalse(is_root)
            self.assertEqual(block1.header.height, 1)
            self.assertEqual(block1.header.branch.value, 0b10)
            self.assertEqual(len(block1.tx_list), 1)

            original_balance_acc1 = call_async(
                master.get_primary_account_data(acc1)
            ).balance
            gas_paid = (opcodes.GTXXSHARDCOST + opcodes.GTXCOST) * 3
            self.assertTrue(
                call_async(
                    master.add_raw_minor_block(block1.header.branch, block1.serialize())
                )
            )
            self.assertEqual(
                call_async(master.get_primary_account_data(acc1)).balance,
                original_balance_acc1 - 54321 - gas_paid,
            )
            self.assertEqual(
                clusters[0].get_shard_state(0b11).get_balance(acc3.recipient), 0
            )

            # Expect to mine root
            is_root, block = call_async(master.get_next_block_to_mine(address=acc2))
            self.assertTrue(is_root)
            self.assertEqual(block.header.height, 2)
            self.assertEqual(len(block.minor_block_header_list), 1)
            self.assertEqual(block.minor_block_header_list[0], block1.header)

            self.assertTrue(master.root_state.add_block(block))
            clusters[0].get_shard_state(0b11).add_root_block(block)
            self.assertEqual(
                clusters[0].get_shard_state(0b11).get_balance(acc3.recipient), 0
            )

            # Mine shard 1
            block3 = (
                clusters[0].get_shard_state(0b11).create_block_to_mine(address=acc4)
            )
            self.assertEqual(block3.header.height, 1)
            self.assertEqual(block3.header.branch.value, 0b11)
            self.assertEqual(len(block3.tx_list), 0)

            self.assertTrue(
                call_async(
                    master.add_raw_minor_block(block3.header.branch, block3.serialize())
                )
            )
            # Expect withdrawTo is included in acc3's balance
            self.assertEqual(
                call_async(master.get_primary_account_data(acc3)).balance, 54321
            )

    def test_get_primary_account_data(self):
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)
        acc2 = Address.create_random_account(full_shard_key=1)

        with ClusterContext(1, acc1) as clusters:
            master = clusters[0].master
            slaves = clusters[0].slave_list

            self.assertEqual(
                call_async(master.get_primary_account_data(acc1)).transaction_count, 0
            )
            tx = create_transfer_transaction(
                shard_state=clusters[0].get_shard_state(0b10),
                key=id1.get_key(),
                from_address=acc1,
                to_address=acc1,
                value=12345,
            )
            self.assertTrue(slaves[0].add_tx(tx))

            is_root, root = call_async(
                master.get_next_block_to_mine(address=acc1, prefer_root=True)
            )
            self.assertTrue(is_root)
            call_async(master.add_root_block(root))

            is_root, block1 = call_async(master.get_next_block_to_mine(address=acc1))
            self.assertFalse(is_root)
            self.assertTrue(
                call_async(
                    master.add_raw_minor_block(block1.header.branch, block1.serialize())
                )
            )

            self.assertEqual(
                call_async(master.get_primary_account_data(acc1)).transaction_count, 1
            )
            self.assertEqual(
                call_async(master.get_primary_account_data(acc2)).transaction_count, 0
            )

    def test_add_transaction(self):
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)
        acc2 = Address.create_from_identity(id1, full_shard_key=1)

        with ClusterContext(2, acc1) as clusters:
            master = clusters[0].master

            tx1 = create_transfer_transaction(
                shard_state=clusters[0].get_shard_state(0b10),
                key=id1.get_key(),
                from_address=acc1,
                to_address=acc1,
                value=12345,
            )
            self.assertTrue(call_async(master.add_transaction(tx1)))
            self.assertEqual(len(clusters[0].get_shard_state(0b10).tx_queue), 1)

            branch1 = Branch(2 | 1)
            tx2 = create_transfer_transaction(
                shard_state=clusters[0].get_shard_state(0b11),
                key=id1.get_key(),
                from_address=acc2,
                to_address=acc1,
                value=12345,
                gas=30000,
            )
            self.assertTrue(call_async(master.add_transaction(tx2)))
            self.assertEqual(len(clusters[0].get_shard_state(0b11).tx_queue), 1)

            # check the tx is received by the other cluster
            tx_queue = clusters[1].get_shard_state(0b10).tx_queue
            assert_true_with_timeout(lambda: len(tx_queue) == 1)
            self.assertEqual(tx_queue.pop_transaction(), tx1.code.get_evm_transaction())

            tx_queue = clusters[1].get_shard_state(0b11).tx_queue
            assert_true_with_timeout(lambda: len(tx_queue) == 1)
            self.assertEqual(tx_queue.pop_transaction(), tx2.code.get_evm_transaction())

    def test_add_minor_block_request_list(self):
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)

        with ClusterContext(2, acc1) as clusters:
            shard_state = clusters[0].get_shard_state(0b10)
            coinbase_amount = (
                shard_state.env.quark_chain_config.shards[
                    shard_state.full_shard_id
                ].COINBASE_AMOUNT
                // 2
            )
            b1 = shard_state.get_tip().create_block_to_append()
            evm_state = shard_state.run_block(b1)
            b1.finalize(
                evm_state=evm_state,
                coinbase_amount=evm_state.block_fee + coinbase_amount,
            )
            add_result = call_async(
                clusters[0].master.add_raw_minor_block(b1.header.branch, b1.serialize())
            )
            self.assertTrue(add_result)

            # Make sure the xshard list is not broadcasted to the other shard
            self.assertFalse(
                clusters[0]
                .get_shard_state(0b11)
                .contain_remote_minor_block_hash(b1.header.get_hash())
            )
            self.assertTrue(
                clusters[0].master.root_state.is_minor_block_validated(
                    b1.header.get_hash()
                )
            )

            # Make sure another cluster received the new block
            assert_true_with_timeout(
                lambda: clusters[0]
                .get_shard_state(0b10)
                .contain_block_by_hash(b1.header.get_hash())
            )
            assert_true_with_timeout(
                lambda: clusters[1].master.root_state.is_minor_block_validated(
                    b1.header.get_hash()
                )
            )

    def test_add_root_block_request_list(self):
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)

        with ClusterContext(2, acc1) as clusters:
            # shutdown cluster connection
            clusters[1].peer.close()

            # add blocks in cluster 0
            block_header_list = [clusters[0].get_shard_state(2 | 0).header_tip]
            shard_state0 = clusters[0].get_shard_state(0b10)
            coinbase_amount = (
                shard_state0.env.quark_chain_config.shards[
                    shard_state0.full_shard_id
                ].COINBASE_AMOUNT
                // 2
            )
            for i in range(7):
                b1 = shard_state0.get_tip().create_block_to_append()
                evm_state = shard_state0.run_block(b1)
                b1.finalize(
                    evm_state=evm_state,
                    coinbase_amount=evm_state.block_fee + coinbase_amount,
                )
                add_result = call_async(
                    clusters[0].master.add_raw_minor_block(
                        b1.header.branch, b1.serialize()
                    )
                )
                self.assertTrue(add_result)
                block_header_list.append(b1.header)

            block_header_list.append(clusters[0].get_shard_state(2 | 1).header_tip)
            shard_state0 = clusters[0].get_shard_state(0b11)
            coinbase_amount = (
                shard_state0.env.quark_chain_config.shards[
                    shard_state0.full_shard_id
                ].COINBASE_AMOUNT
                // 2
            )
            b2 = shard_state0.get_tip().create_block_to_append()
            evm_state = shard_state0.run_block(b2)
            b2.finalize(
                evm_state=evm_state,
                coinbase_amount=evm_state.block_fee + coinbase_amount,
            )
            add_result = call_async(
                clusters[0].master.add_raw_minor_block(b2.header.branch, b2.serialize())
            )
            self.assertTrue(add_result)
            block_header_list.append(b2.header)

            # add 1 block in cluster 1
            shard_state1 = clusters[1].get_shard_state(0b11)
            coinbase_amount = (
                shard_state1.env.quark_chain_config.shards[
                    shard_state1.full_shard_id
                ].COINBASE_AMOUNT
                // 2
            )
            b3 = shard_state1.get_tip().create_block_to_append()
            evm_state = shard_state1.run_block(b3)
            b3.finalize(
                evm_state=evm_state,
                coinbase_amount=evm_state.block_fee + coinbase_amount,
            )
            add_result = call_async(
                clusters[1].master.add_raw_minor_block(b3.header.branch, b3.serialize())
            )
            self.assertTrue(add_result)

            self.assertEqual(clusters[1].get_shard_state(0b11).header_tip, b3.header)

            # reestablish cluster connection
            call_async(
                clusters[1].network.connect(
                    "127.0.0.1",
                    clusters[0].master.env.cluster_config.SIMPLE_NETWORK.BOOTSTRAP_PORT,
                )
            )

            root_block1 = clusters[0].master.root_state.create_block_to_mine(
                block_header_list, acc1
            )
            call_async(clusters[0].master.add_root_block(root_block1))

            # Make sure the root block tip of local cluster is changed
            self.assertEqual(clusters[0].master.root_state.tip, root_block1.header)

            # Make sure the root block tip of cluster 1 is changed
            assert_true_with_timeout(
                lambda: clusters[1].master.root_state.tip == root_block1.header, 2
            )

            # Minor block is downloaded
            self.assertEqual(b1.header.height, 7)
            assert_true_with_timeout(
                lambda: clusters[1].get_shard_state(0b10).header_tip == b1.header
            )

            # The tip is overwritten due to root chain first consensus
            assert_true_with_timeout(
                lambda: clusters[1].get_shard_state(0b11).header_tip == b2.header
            )

    def test_shard_synchronizer_with_fork(self):
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)

        with ClusterContext(2, acc1) as clusters:
            # shutdown cluster connection
            clusters[1].peer.close()

            block_list = []
            # cluster 0 has 13 blocks added
            shard_state0 = clusters[0].get_shard_state(0b10)
            coinbase_amount = (
                shard_state0.env.quark_chain_config.shards[
                    shard_state0.full_shard_id
                ].COINBASE_AMOUNT
                // 2
            )
            for i in range(13):
                block = shard_state0.get_tip().create_block_to_append()
                evm_state = shard_state0.run_block(block)
                block.finalize(
                    evm_state=evm_state,
                    coinbase_amount=evm_state.block_fee + coinbase_amount,
                )
                add_result = call_async(
                    clusters[0].master.add_raw_minor_block(
                        block.header.branch, block.serialize()
                    )
                )
                self.assertTrue(add_result)
                block_list.append(block)
            self.assertEqual(clusters[0].get_shard_state(0b10).header_tip.height, 13)

            # cluster 1 has 12 blocks added
            shard_state0 = clusters[1].get_shard_state(0b10)
            coinbase_amount = (
                shard_state0.env.quark_chain_config.shards[
                    shard_state0.full_shard_id
                ].COINBASE_AMOUNT
                // 2
            )
            for i in range(12):
                block = shard_state0.get_tip().create_block_to_append()
                evm_state = shard_state0.run_block(block)
                block.finalize(
                    evm_state=evm_state,
                    coinbase_amount=evm_state.block_fee + coinbase_amount,
                )
                add_result = call_async(
                    clusters[1].master.add_raw_minor_block(
                        block.header.branch, block.serialize()
                    )
                )
                self.assertTrue(add_result)
            self.assertEqual(clusters[1].get_shard_state(0b10).header_tip.height, 12)

            # reestablish cluster connection
            call_async(
                clusters[1].network.connect(
                    "127.0.0.1",
                    clusters[0].master.env.cluster_config.SIMPLE_NETWORK.BOOTSTRAP_PORT,
                )
            )

            # a new block from cluster 0 will trigger sync in cluster 1
            shard_state0 = clusters[0].get_shard_state(0b10)
            coinbase_amount = (
                shard_state0.env.quark_chain_config.shards[
                    shard_state0.full_shard_id
                ].COINBASE_AMOUNT
                // 2
            )
            block = shard_state0.get_tip().create_block_to_append()
            evm_state = shard_state0.run_block(block)
            block.finalize(
                evm_state=evm_state,
                coinbase_amount=evm_state.block_fee + coinbase_amount,
            )
            add_result = call_async(
                clusters[0].master.add_raw_minor_block(
                    block.header.branch, block.serialize()
                )
            )
            self.assertTrue(add_result)
            block_list.append(block)

            # expect cluster 1 has all the blocks from cluter 0 and
            # has the same tip as cluster 0
            for block in block_list:
                assert_true_with_timeout(
                    lambda: clusters[1]
                    .slave_list[0]
                    .shards[Branch(0b10)]
                    .state.contain_block_by_hash(block.header.get_hash())
                )
                assert_true_with_timeout(
                    lambda: clusters[1].master.root_state.is_minor_block_validated(
                        block.header.get_hash()
                    )
                )

            self.assertEqual(
                clusters[1].get_shard_state(0b10).header_tip,
                clusters[0].get_shard_state(0b10).header_tip,
            )

    def test_shard_genesis_fork_fork(self):
        """ Test shard forks at genesis blocks due to root chain fork at GENESIS.ROOT_HEIGHT"""
        acc1 = Address.create_random_account(0)
        acc2 = Address.create_random_account(1)

        genesis_root_heights = {2: 0, 3: 1}
        with ClusterContext(
            2,
            acc1,
            chain_size=1,
            shard_size=2,
            genesis_root_heights=genesis_root_heights,
        ) as clusters:
            # shutdown cluster connection
            clusters[1].peer.close()

            master0 = clusters[0].master
            is_root, root0 = call_async(master0.get_next_block_to_mine(acc1))
            self.assertTrue(is_root)
            call_async(master0.add_root_block(root0))
            genesis0 = (
                clusters[0].get_shard_state(2 | 1).db.get_minor_block_by_height(0)
            )
            self.assertEqual(
                genesis0.header.hash_prev_root_block, root0.header.get_hash()
            )

            master1 = clusters[1].master
            is_root, root1 = call_async(master1.get_next_block_to_mine(acc2))
            self.assertTrue(is_root)
            self.assertNotEqual(root0.header.get_hash(), root1.header.get_hash())
            call_async(master1.add_root_block(root1))
            genesis1 = (
                clusters[1].get_shard_state(2 | 1).db.get_minor_block_by_height(0)
            )
            self.assertEqual(
                genesis1.header.hash_prev_root_block, root1.header.get_hash()
            )

            # let's make cluster1's root chain longer than cluster0's
            is_root, root2 = call_async(master1.get_next_block_to_mine(acc2))
            self.assertTrue(is_root)
            call_async(master1.add_root_block(root2))
            self.assertEqual(master1.root_state.tip.height, 2)

            # reestablish cluster connection
            call_async(
                clusters[1].network.connect(
                    "127.0.0.1",
                    clusters[0].master.env.cluster_config.SIMPLE_NETWORK.BOOTSTRAP_PORT,
                )
            )
            # Expect cluster0's genesis change to genesis1
            assert_true_with_timeout(
                lambda: clusters[0]
                .get_shard_state(2 | 1)
                .db.get_minor_block_by_height(0)
                .header.get_hash()
                == genesis1.header.get_hash()
            )
            self.assertTrue(clusters[0].get_shard_state(2 | 1).root_tip == root2.header)

    def test_broadcast_cross_shard_transactions(self):
        """ Test the cross shard transactions are broadcasted to the destination shards """
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)
        acc3 = Address.create_random_account(full_shard_key=1)

        with ClusterContext(1, acc1) as clusters:
            master = clusters[0].master
            slaves = clusters[0].slave_list

            # Add a root block first so that later minor blocks referring to this root
            # can be broadcasted to other shards
            is_root, root_block = call_async(
                master.get_next_block_to_mine(
                    Address.create_empty_account(), prefer_root=True
                )
            )
            self.assertTrue(is_root)
            call_async(master.add_root_block(root_block))

            tx1 = create_transfer_transaction(
                shard_state=clusters[0].get_shard_state(2 | 0),
                key=id1.get_key(),
                from_address=acc1,
                to_address=acc3,
                value=54321,
                gas=opcodes.GTXXSHARDCOST + opcodes.GTXCOST,
            )
            self.assertTrue(slaves[0].add_tx(tx1))

            b1 = clusters[0].get_shard_state(2 | 0).create_block_to_mine(address=acc1)
            b2 = clusters[0].get_shard_state(2 | 0).create_block_to_mine(address=acc1)
            b2.header.create_time += 1
            self.assertNotEqual(b1.header.get_hash(), b2.header.get_hash())

            call_async(clusters[0].get_shard(2 | 0).add_block(b1))

            # expect shard 1 got the CrossShardTransactionList of b1
            xshard_tx_list = (
                clusters[0]
                .get_shard_state(2 | 1)
                .db.get_minor_block_xshard_tx_list(b1.header.get_hash())
            )
            self.assertEqual(len(xshard_tx_list.tx_list), 1)
            self.assertEqual(xshard_tx_list.tx_list[0].tx_hash, tx1.get_hash())
            self.assertEqual(xshard_tx_list.tx_list[0].from_address, acc1)
            self.assertEqual(xshard_tx_list.tx_list[0].to_address, acc3)
            self.assertEqual(xshard_tx_list.tx_list[0].value, 54321)

            call_async(clusters[0].get_shard(2 | 0).add_block(b2))
            # b2 doesn't update tip
            self.assertEqual(clusters[0].get_shard_state(2 | 0).header_tip, b1.header)

            # expect shard 1 got the CrossShardTransactionList of b2
            xshard_tx_list = (
                clusters[0]
                .get_shard_state(2 | 1)
                .db.get_minor_block_xshard_tx_list(b2.header.get_hash())
            )
            self.assertEqual(len(xshard_tx_list.tx_list), 1)
            self.assertEqual(xshard_tx_list.tx_list[0].tx_hash, tx1.get_hash())
            self.assertEqual(xshard_tx_list.tx_list[0].from_address, acc1)
            self.assertEqual(xshard_tx_list.tx_list[0].to_address, acc3)
            self.assertEqual(xshard_tx_list.tx_list[0].value, 54321)

            b3 = (
                clusters[0]
                .get_shard_state(2 | 1)
                .create_block_to_mine(address=acc1.address_in_shard(1))
            )
            call_async(master.add_raw_minor_block(b3.header.branch, b3.serialize()))

            is_root, root_block = call_async(
                master.get_next_block_to_mine(address=acc1)
            )
            self.assertTrue(is_root)
            call_async(master.add_root_block(root_block))

            # b4 should include the withdraw of tx1
            b4 = (
                clusters[0]
                .get_shard_state(2 | 1)
                .create_block_to_mine(address=acc1.address_in_shard(1))
            )

            # adding b1, b2, b3 again shouldn't affect b4 to be added later
            self.assertTrue(
                call_async(master.add_raw_minor_block(b1.header.branch, b1.serialize()))
            )
            self.assertTrue(
                call_async(master.add_raw_minor_block(b2.header.branch, b2.serialize()))
            )
            self.assertTrue(
                call_async(master.add_raw_minor_block(b3.header.branch, b3.serialize()))
            )
            self.assertTrue(
                call_async(master.add_raw_minor_block(b4.header.branch, b4.serialize()))
            )
            self.assertEqual(
                call_async(master.get_primary_account_data(acc3)).balance, 54321
            )

    def test_broadcast_cross_shard_transactions_to_neighbor_only(self):
        """ Test the broadcast is only done to the neighbors """
        id1 = Identity.create_random_identity()
        acc1 = Address.create_from_identity(id1, full_shard_key=0)

        # create 64 shards so that the neighbor rule can kick in
        # explicitly set num_slaves to 4 so that it does not spin up 64 slaves
        with ClusterContext(1, acc1, shard_size=64, num_slaves=4) as clusters:
            master = clusters[0].master

            # Add a root block first so that later minor blocks referring to this root
            # can be broadcasted to other shards
            is_root, root_block = call_async(
                master.get_next_block_to_mine(
                    Address.create_empty_account(), prefer_root=True
                )
            )
            self.assertTrue(is_root)
            call_async(master.add_root_block(root_block))

            b1 = clusters[0].get_shard_state(64).create_block_to_mine(address=acc1)
            self.assertTrue(
                call_async(master.add_raw_minor_block(b1.header.branch, b1.serialize()))
            )

            neighbor_shards = [2 ** i for i in range(6)]
            for shard_id in range(64):
                xshard_tx_list = (
                    clusters[0]
                    .get_shard_state(64 | shard_id)
                    .db.get_minor_block_xshard_tx_list(b1.header.get_hash())
                )
                # Only neighbor should have it
                if shard_id in neighbor_shards:
                    self.assertIsNotNone(xshard_tx_list)
                else:
                    self.assertIsNone(xshard_tx_list)
