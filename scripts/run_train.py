###########################################################################################
# Training script for MACE
# Authors: Ilyes Batatia, Gregor Simm, David Kovacs
# This program is distributed under the MIT License (see MIT.md)
###########################################################################################

import ast
import logging
from pathlib import Path
from typing import Optional
import json
import os

import numpy as np
import torch.nn.functional
from e3nn import o3
from torch.optim.swa_utils import SWALR, AveragedModel
from torch_ema import ExponentialMovingAverage
from accelerate import Accelerator

import mace
from mace import data, modules, tools
from mace.tools import torch_geometric
from mace.tools.scripts_utils import (
    create_error_table,
    get_dataset_from_xyz,
    get_atomic_energies,
    get_config_type_weights,
    get_loss_fn,
    get_files_with_suffix,
)
from mace.data import HDF5Dataset

def main() -> None:
    accelerator = Accelerator()
    args = tools.build_default_arg_parser().parse_args()
    tag = tools.get_tag(name=args.name, seed=args.seed)

    # Setup
    tools.set_seeds(args.seed)
    tools.setup_logger(accelerator, level=args.log_level, tag=tag, directory=args.log_dir)
    
    try:
        logging.info(f"MACE version: {mace.__version__}")
    except AttributeError:
        logging.info("Cannot find MACE version, please install MACE via pip")
    logging.info(f"Configuration: {args}")
    
    tools.set_default_dtype(args.default_dtype)

    if args.statistics_file is not None:
        with open(args.statistics_file, "r") as f:
            statistics = json.load(f)
        logging.info("Using statistics json file")
        args.r_max = statistics["r_max"]
        args.atomic_numbers = statistics["atomic_numbers"]
        args.mean = statistics["mean"]
        args.std = statistics["std"]
        args.avg_num_neighbors = statistics["avg_num_neighbors"]
        args.compute_avg_num_neighbors = False
        args.E0s = statistics["atomic_energies"]

    # Data preparation
    if args.train_file.endswith(".xyz"):
        if args.valid_file is not None:
            assert args.valid_file.endswith(
                ".xyz"
            ), "valid_file if given must be same format as train_file"
        config_type_weights = get_config_type_weights(args.config_type_weights)
        collections, atomic_energies_dict = get_dataset_from_xyz(
            train_path=args.train_file,
            valid_path=args.valid_file,
            valid_fraction=args.valid_fraction,
            config_type_weights=config_type_weights,
            test_path=args.test_file,
            seed=args.seed,
            energy_key=args.energy_key,
            forces_key=args.forces_key,
            stress_key=args.stress_key,
            virials_key=args.virials_key,
            dipole_key=args.dipole_key,
            charges_key=args.charges_key,
        )

        logging.info(
            f"Total number of configurations: train={len(collections.train)}, valid={len(collections.valid)}, "
            f"tests=[{', '.join([name + ': ' + str(len(test_configs)) for name, test_configs in collections.tests])}]"
        )
    elif args.train_file.endswith(".h5"):
        atomic_energies_dict = None
    else:
        raise RuntimeError(
            f"train_file must be either .xyz or .h5, got {args.train_file}"
        )

    # Atomic number table
    # yapf: disable
    if args.atomic_numbers is None:
        assert args.train_file.endswith(".xyz"), "Must specify atomic_numbers when using .h5 train_file input"
        z_table = tools.get_atomic_number_table_from_zs(
            z
            for configs in (collections.train, collections.valid)
            for config in configs
            for z in config.atomic_numbers
        )
    else:
        if args.statistics_file is None:
            logging.info("Using atomic numbers from command line argument")
        else:
            logging.info("Using atomic numbers from statistics file")
        zs_list = ast.literal_eval(args.atomic_numbers)
        assert isinstance(zs_list, list)
        z_table = tools.get_atomic_number_table_from_zs(zs_list)
    # yapf: enable
    logging.info(z_table)

    if atomic_energies_dict is None or len(atomic_energies_dict) == 0:
        if args.train_file.endswith(".xyz"):
            atomic_energies_dict = get_atomic_energies(
                args.E0s, collections.train, z_table
            )
        else:
            atomic_energies_dict = get_atomic_energies(args.E0s, None, z_table)

    if args.model == "AtomicDipolesMACE":
        atomic_energies = None
        dipole_only = True
        compute_dipole = True
        compute_energy = False
        args.compute_forces = False
        compute_virials = False
        args.compute_stress = False
    else:
        dipole_only = False
        if args.model == "EnergyDipolesMACE":
            compute_dipole = True
            compute_energy = True
            args.compute_forces = True
            compute_virials = False
            args.compute_stress = False
        else:
            compute_energy = True
            compute_dipole = False

        atomic_energies: np.ndarray = np.array(
            [atomic_energies_dict[z] for z in z_table.zs]
        )
        logging.info(f"Atomic energies: {atomic_energies.tolist()}")

    if args.train_file.endswith(".xyz"):
        train_set = [
            data.AtomicData.from_config(config, z_table=z_table, cutoff=args.r_max)
            for config in collections.train
        ]
        valid_set = [
            data.AtomicData.from_config(config, z_table=z_table, cutoff=args.r_max)
            for config in collections.valid
        ]
    else:
        train_set = HDF5Dataset(
            args.train_file, r_max=args.r_max, z_table=z_table
        )
        valid_set = HDF5Dataset(
            args.valid_file, r_max=args.r_max, z_table=z_table
        )
        
    train_loader = torch_geometric.dataloader.DataLoader(
        dataset=train_set,
        batch_size=args.batch_size,   
        shuffle=True,
        drop_last=False,
        pin_memory=args.pin_memory,
        num_workers=args.num_workers,
    )
    valid_loader = torch_geometric.dataloader.DataLoader(
        dataset=valid_set,
        batch_size=args.valid_batch_size,
        shuffle=True,
        drop_last=False,
        pin_memory=args.pin_memory,
        num_workers=args.num_workers,
    )
    
    loss_fn: torch.nn.Module = get_loss_fn(
        args.loss,
        args.energy_weight,
        args.forces_weight,
        args.angle_weight,
        args.stress_weight,
        args.virials_weight,
        args.dipole_weight,
        dipole_only,
        compute_dipole,
    )
    logging.info(loss_fn)

    if args.compute_avg_num_neighbors:
        args.avg_num_neighbors = modules.compute_avg_num_neighbors(train_loader)
    logging.info(f"Average number of neighbors: {args.avg_num_neighbors}")

    # Selecting outputs
    compute_virials = False
    if args.loss in ("stress", "virials"):
        compute_virials = True
        args.compute_stress = True
        args.error_table = "PerAtomRMSEstressvirials"

    output_args = {
        "energy": compute_energy,
        "forces": args.compute_forces,
        "virials": compute_virials,
        "stress": args.compute_stress,
        "dipoles": compute_dipole,
    }
    logging.info(f"Selected the following outputs: {output_args}")

    # Build model
    logging.info("Building model")
    if args.num_channels is not None and args.max_L is not None:
        assert args.num_channels > 0, "num_channels must be positive integer"
        assert (
            args.max_L >= 0 and args.max_L < 4
        ), "max_L must be between 0 and 3, if you want to use larger specify it via the hidden_irrpes keyword"
        args.hidden_irreps = f"{args.num_channels:d}x0e"
        if args.max_L > 0:
            args.hidden_irreps += f" + {args.num_channels:d}x1o"
        if args.max_L > 1:
            args.hidden_irreps += f" + {args.num_channels:d}x2e"
        if args.max_L > 2:
            args.hidden_irreps += f" + {args.num_channels:d}x3o"
    logging.info(f"Hidden irreps: {args.hidden_irreps}")

    model_config = dict(
        r_max=args.r_max,
        num_bessel=args.num_radial_basis,
        num_polynomial_cutoff=args.num_cutoff_basis,
        max_ell=args.max_ell,
        interaction_cls=modules.interaction_classes[args.interaction],
        num_interactions=args.num_interactions,
        num_elements=len(z_table),
        hidden_irreps=o3.Irreps(args.hidden_irreps),
        atomic_energies=atomic_energies,
        avg_num_neighbors=args.avg_num_neighbors,
        atomic_numbers=z_table.zs,
    )

    model: torch.nn.Module

    if args.scaling == "no_scaling":
        args.std = 1.0
        logging.info("No scaling selected")
    elif args.mean is None or args.std is None:
        args.mean, args.std = modules.scaling_classes[args.scaling](
            train_loader, atomic_energies
        )

    if args.model == "MACE":
        model = modules.ScaleShiftMACE(
            **model_config,
            correlation=args.correlation,
            gate=modules.gate_dict[args.gate],
            interaction_cls_first=modules.interaction_classes[
                "RealAgnosticInteractionBlock"
            ],
            MLP_irreps=o3.Irreps(args.MLP_irreps),
            atomic_inter_scale=args.std,
            atomic_inter_shift=0.0,
        )
    elif args.model == "ScaleShiftMACE":
        model = modules.ScaleShiftMACE(
            **model_config,
            correlation=args.correlation,
            gate=modules.gate_dict[args.gate],
            interaction_cls_first=modules.interaction_classes[args.interaction_first],
            MLP_irreps=o3.Irreps(args.MLP_irreps),
            atomic_inter_scale=args.std,
            atomic_inter_shift=args.mean,
        )
    elif args.model == "ScaleShiftBOTNet":
        model = modules.ScaleShiftBOTNet(
            **model_config,
            gate=modules.gate_dict[args.gate],
            interaction_cls_first=modules.interaction_classes[args.interaction_first],
            MLP_irreps=o3.Irreps(args.MLP_irreps),
            atomic_inter_scale=args.std,
            atomic_inter_shift=args.mean,
        )
    elif args.model == "BOTNet":
        model = modules.BOTNet(
            **model_config,
            gate=modules.gate_dict[args.gate],
            interaction_cls_first=modules.interaction_classes[args.interaction_first],
            MLP_irreps=o3.Irreps(args.MLP_irreps),
        )
    elif args.model == "AtomicDipolesMACE":
        # std_df = modules.scaling_classes["rms_dipoles_scaling"](train_loader)
        assert args.loss == "dipole", "Use dipole loss with AtomicDipolesMACE model"
        assert (
            args.error_table == "DipoleRMSE"
        ), "Use error_table DipoleRMSE with AtomicDipolesMACE model"
        model = modules.AtomicDipolesMACE(
            **model_config,
            correlation=args.correlation,
            gate=modules.gate_dict[args.gate],
            interaction_cls_first=modules.interaction_classes[
                "RealAgnosticInteractionBlock"
            ],
            MLP_irreps=o3.Irreps(args.MLP_irreps),
            # dipole_scale=1,
            # dipole_shift=0,
        )
    elif args.model == "EnergyDipolesMACE":
        # std_df = modules.scaling_classes["rms_dipoles_scaling"](train_loader)
        assert (
            args.loss == "energy_forces_dipole"
        ), "Use energy_forces_dipole loss with EnergyDipolesMACE model"
        assert (
            args.error_table == "EnergyDipoleRMSE"
        ), "Use error_table EnergyDipoleRMSE with AtomicDipolesMACE model"
        model = modules.EnergyDipolesMACE(
            **model_config,
            correlation=args.correlation,
            gate=modules.gate_dict[args.gate],
            interaction_cls_first=modules.interaction_classes[
                "RealAgnosticInteractionBlock"
            ],
            MLP_irreps=o3.Irreps(args.MLP_irreps),
        )
    else:
        raise RuntimeError(f"Unknown model: '{args.model}'")

    if args.load_model is not None:
        print(f'Loading model from {args.load_model}...')
        model = torch.load(args.load_model)

    # Optimizer
    decay_interactions = {}
    no_decay_interactions = {}
    for name, param in model.interactions.named_parameters():
        if "linear.weight" in name or "skip_tp_full.weight" in name:
            decay_interactions[name] = param
        else:
            no_decay_interactions[name] = param

    param_options = dict(
        params=[
            {
                "name": "embedding",
                "params": model.node_embedding.parameters(),
                "weight_decay": 0.0,
            },
            {
                "name": "interactions_decay",
                "params": list(decay_interactions.values()),
                "weight_decay": args.weight_decay,
            },
            {
                "name": "interactions_no_decay",
                "params": list(no_decay_interactions.values()),
                "weight_decay": 0.0,
            },
            {
                "name": "products",
                "params": model.products.parameters(),
                "weight_decay": args.weight_decay,
            },
            {
                "name": "readouts",
                "params": model.readouts.parameters(),
                "weight_decay": 0.0,
            },
        ],
        lr=args.lr,
        amsgrad=args.amsgrad,
    )

    optimizer: torch.optim.Optimizer
    if args.optimizer == "adamw":
        optimizer = torch.optim.AdamW(**param_options)
    else:
        optimizer = torch.optim.Adam(**param_options)

    logger = tools.MetricsLogger(directory=args.results_dir, tag=tag + "_train")

    if args.scheduler == "ExponentialLR":
        lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(
            optimizer=optimizer, gamma=args.lr_scheduler_gamma
        )
    elif args.scheduler == "ReduceLROnPlateau":
        lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer=optimizer,
            factor=args.lr_factor,
            patience=args.scheduler_patience,
        )
    else:
        raise RuntimeError(f"Unknown scheduler: '{args.scheduler}'")

    swa: Optional[tools.SWAContainer] = None
    swas = [False]
    if args.swa:
        assert dipole_only is False, "swa for dipole fitting not implemented"
        swas.append(True)
        if args.start_swa is None:
            args.start_swa = (
                args.max_num_epochs // 4 * 3
            )  # if not set start swa at 75% of training
        if args.loss == "forces_only":
            logging.info("Can not select swa with forces only loss.")
        elif args.loss == "virials":
            loss_fn_energy = modules.WeightedEnergyForcesVirialsLoss(
                energy_weight=args.swa_energy_weight,
                forces_weight=args.swa_forces_weight,
                virials_weight=args.swa_virials_weight,
            )
        elif args.loss == "stress":
            loss_fn_energy = modules.WeightedEnergyForcesStressLoss(
                energy_weight=args.swa_energy_weight,
                forces_weight=args.swa_forces_weight,
                stress_weight=args.swa_stress_weight,
            )
        elif args.loss == "energy_forces_dipole":
            loss_fn_energy = modules.WeightedEnergyForcesDipoleLoss(
                args.swa_energy_weight,
                forces_weight=args.swa_forces_weight,
                dipole_weight=args.swa_dipole_weight,
            )
            logging.info(
                f"Using stochastic weight averaging (after {args.start_swa} epochs) with energy weight : {args.swa_energy_weight}, forces weight : {args.swa_forces_weight}, dipole weight : {args.swa_dipole_weight} and learning rate : {args.swa_lr}"
            )
        else:
            loss_fn_energy = modules.WeightedEnergyForcesLoss(
                energy_weight=args.swa_energy_weight,
                forces_weight=args.swa_forces_weight,
            )
            logging.info(
                f"Using stochastic weight averaging (after {args.start_swa} epochs) with energy weight : {args.swa_energy_weight}, forces weight : {args.swa_forces_weight} and learning rate : {args.swa_lr}"
            )
        swa = tools.SWAContainer(
            model=AveragedModel(model),
            scheduler=SWALR(
                optimizer=optimizer,
                swa_lr=args.swa_lr,
                anneal_epochs=1,
                anneal_strategy="linear",
            ),
            start=args.start_swa,
            loss_fn=loss_fn_energy,
        )

    model, optimizer, train_loader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_loader, lr_scheduler
    )

    checkpoint_handler = tools.CheckpointHandler(
        directory=args.checkpoints_dir,
        tag=tag,
        keep=args.keep_checkpoints,
        swa_start=args.start_swa,
        accelerator=accelerator,
    )

    start_epoch = 0
    if args.restart_latest:
        try:
            opt_start_epoch = checkpoint_handler.load_latest(
                swa=True,
            )
        except:
            opt_start_epoch = checkpoint_handler.load_latest(
                swa=False,
            )
        if opt_start_epoch is not None:
            start_epoch = opt_start_epoch

        # Allow the user to manually adjust the learning rate
        if args.restart_lr is not None:
            for g in optimizer.param_groups:
                g['lr'] = args.restart_lr

    ema: Optional[ExponentialMovingAverage] = None
    if args.ema:
        ema = ExponentialMovingAverage(model.parameters(), decay=args.ema_decay)

    #logging.info(model)
    logging.info(f"Number of parameters: {tools.count_parameters(model)}")
    logging.info(f"Optimizer: {optimizer}")

    if args.wandb:
        logging.info("Using Weights and Biases for logging")
        import wandb

        wandb_config = {}
        args_dict = vars(args)
        args_dict_json = json.dumps(args_dict)
        for key in args.wandb_log_hypers:
            wandb_config[key] = args_dict[key]
        tools.init_wandb(
            project=args.wandb_project,
            entity=args.wandb_entity,
            name=args.wandb_name,
            config=wandb_config,
        )
        wandb.run.summary["params"] = args_dict_json

    tools.train(
        accelerator=accelerator,
        model=model,
        loss_fn=loss_fn,
        train_loader=train_loader,
        valid_loader=valid_loader,
        optimizer=optimizer,
        lr_scheduler=lr_scheduler,
        checkpoint_handler=checkpoint_handler,
        eval_interval=args.eval_interval,
        start_epoch=start_epoch,
        max_num_epochs=args.max_num_epochs,
        logger=logger,
        patience=args.patience,
        output_args=output_args,
        swa=swa,
        ema=ema,
        max_grad_norm=args.clip_grad,
        log_errors=args.error_table,
        log_wandb=args.wandb,
    )

    logging.info("Computing metrics for training, validation, and test sets")
    
    all_data_loaders = {
        "train": train_loader,
        "valid": valid_loader,
    }

    test_sets = {}
    if args.train_file.endswith(".xyz"):
        for name, subset in collections.tests:
            test_sets[name] = [
                data.AtomicData.from_config(config, z_table=z_table, cutoff=args.r_max)
                for config in subset
            ]
    else:
        test_files = get_files_with_suffix(args.test_dir, "_test.h5")
        for test_file in test_files:
            name = os.path.splitext(os.path.basename(test_file))[0]
            test_sets[name] = HDF5Dataset(test_file, r_max=args.r_max, z_table=z_table)
            
    for test_name, test_set in test_sets.items():
        test_loader = torch_geometric.dataloader.DataLoader(
            test_set,
            batch_size=args.valid_batch_size,
            shuffle=True,
            drop_last=test_set.drop_last,
            num_workers=args.num_workers,
            pin_memory=args.pin_memory,
        )
        all_data_loaders[test_name] = test_loader


    for swa_eval in swas:
        epoch = checkpoint_handler.load_latest(
            swa=swa_eval,
        )

        logging.info(f"Loaded model from epoch {epoch}")

        table = create_error_table(
            table_type=args.error_table,
            all_data_loaders=all_data_loaders,
            model=model,
            loss_fn=loss_fn,
            output_args=output_args,
            log_wandb=args.wandb,
            device=accelerator.device,
        )
        logging.info("\n" + str(table))
    
        # Save entire model
        if swa_eval:
            model_path = Path(args.checkpoints_dir) / (tag + "_swa.model")
        else:
            model_path = Path(args.checkpoints_dir) / (tag + ".model")

        logging.info(f"Saving model to {model_path}")

        if accelerator.process_index == 0:

            if args.save_cpu:
                model = model.to("cpu")

            torch.save(accelerator.unwrap_model(model), model_path)

            if swa_eval:
                torch.save(accelerator.unwrap_model(model), Path(args.model_dir) / (args.name + "_swa.model"))
            else:
                torch.save(accelerator.unwrap_model(model), Path(args.model_dir) / (args.name + ".model"))
                

    logging.info("Done")

if __name__ == "__main__":
    main()
