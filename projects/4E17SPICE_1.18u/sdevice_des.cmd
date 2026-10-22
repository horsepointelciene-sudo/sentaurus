Device PPL_Device {

    Electrode {
        { Name="source" Voltage=0.0 }
        { Name="gate"   Voltage=0.0 Material="PolySi" }
        { Name="drain"  Voltage=0.0 }
    }
    RayTraceBC {
		{ Name= "gate"
			Reflectivity= 0
		}
		{ Name= "source"
			Reflectivity= 0
		}
		{ Name= "drain"
			Reflectivity= 0
		}		
	
	}
    File {
        Grid      = "PPL.tdr"
        Parameter = "SiC.par"
        Current   = "@plot@"
        Plot      = "@tdrdat@"
    }

    Physics {
        AreaFactor = @<AF*44.0/3152.0>@
        Temperature = @Tamb@
	Fermi
	EffectiveIntrinsicDensity ( 
		BandGapNarrowing(OldSlotboom)
		NoFermi 
	)
	Recombination (
		SRH(DopingDependence TempDependence)
		Auger 
	)  
	Mobility ( 	
		Enormal(IALMob(SiC))		
		HighFieldSaturation
		IncompleteIonization		
	)
	IncompleteIonization (
		Split (
			Doping = "NitrogenConcentration"
			Weights = (0.5 0.5)
		)
	)
	Aniso (
		Mobility 
		direction(SimulationSystem) = (1,0,0)
	)
    
    * Retain the original interface physics and optical BCs.
    Optics (
    	ComplexRefractiveIndex (WavelengthDep(Real Imag))
		OpticalGeneration (
			QuantumYield (StepFunction (EffectiveBandgap)) * number of generated carriers per photon, default: 1
			ComputeFromMonochromaticSource
			TimeDependence(
				WaveTime= (50,200)
				WaveTSlope= 1
				WaveTLin= 0.1
				Scaling= 1)
				
		)  * end OpticalGeneration
		
		
		Excitation (
			Wavelength= @lambda@  *<um>				* Incident light wavelength [um]
			Intensity= 0				* Incident light intensity [W/cm2]	
			Polarization= 0.5				* Unpolarized light
			Theta= 0						* Normal incidence,	in -ve y direction
			Window(
				Origin= (-0.05, -2.5)			* Shift origin 0.1 um below the bottom photodiode surface
				Line(x1= 0 x2= 1.5)		* Illumination window covers width of photodiode. 

			)			
			Window(
				Origin= (-0.05, -2.5)			* Shift origin 0.1 um below the bottom photodiode surface
				Line(x1= 2.5 x2= 4)		* Illumination window covers width of photodiode. 

			)			

		) * end Excitation
		
		OpticalSolver (
      		RayTracing (	        		
				RayDistribution(
					Mode= AutoPopulate
					NumberOfRays= 500				* Number of rays in the illumination window
				)
				CompactMemoryOption
				DepthLimit= 1000 					* Stop tracing a ray after passing through more than x material boundaries
				MinIntensity= 1e-5					* Stop tracing a ray when its intensity becomes less than x times the original intensity			
			) * end RayTracing
		)	* end OpticalSolver		
	) * end Optics
	}
}


Device NOPPL_Device {
    Electrode {
        { Name="source" Voltage=0.0 }
        { Name="gate" Voltage=0.0 Material="PolySi" }
        { Name="drain" Voltage=0.0 }
    }
#if @int@ > 0
    RayTraceBC {
        { Name="gate" Reflectivity=0 }
        { Name="source" Reflectivity=0 }
        { Name="drain" Reflectivity=0 }
    }
#endif
    File {
        Grid = "NOPPL.tdr"
        Parameter = "SiC.par"
        Current = "@plot@"
        Plot = "@tdrdat@"
    }
    Physics {
        AreaFactor = @<AF*3108.0/3152.0>@
        Temperature = @Tamb@
        Fermi
        EffectiveIntrinsicDensity (
            BandGapNarrowing(OldSlotboom)
            NoFermi
        )
        Recombination (
            SRH(DopingDependence TempDependence)
            Auger
        )
        Mobility (
            Enormal(IALMob(SiC))
            HighFieldSaturation
            IncompleteIonization
        )
        IncompleteIonization (
            Split (
                Doping = "NitrogenConcentration"
                Weights = (0.5 0.5)
            )
        )
        Aniso (
            Mobility
            direction(SimulationSystem) = (1,0,0)
        )
#if @int@ > 0
        Optics (
            ComplexRefractiveIndex (WavelengthDep(Real Imag))
            OpticalGeneration (
                QuantumYield (StepFunction (EffectiveBandgap))
                ComputeFromMonochromaticSource
                TimeDependence (
                    WaveTime = (50,200)
                    WaveTLin = 0.1
                    Scaling = 1
                )
            )
            Excitation (
                Wavelength = @lambda@   * [um]; use 0.36 for 360 nm
                Intensity = 0          * [W/cm2]; ramped in Solve
                Polarization = 0.5
                Theta = 0              * +y in the 2D DFISE display convention
                Window (
                    Origin = (-0.05, -2.5)
                    Line(x1=0 x2=1.5)
                )
                Window (
                    Origin = (-0.05, -2.5)
                    Line(x1=2.5 x2=4)
                )
            )
            OpticalSolver (
                RayTracing (
                    RayDistribution (
                        Mode = AutoPopulate
                        NumberOfRays = 500
                    )
                    CompactMemoryOption
                    DepthLimit = 1000
                    MinIntensity = 1e-5
                )
            )
        )
#endif
    }
}


File {
    Output = "@log@"
}

Math {
    Digits = 7
    ErrRef(electron) = 1e5
    ErrRef(hole) = 1e5
    RHSmin = 1e-12
    CheckRhsAfterUpdate
    RHSmax = 1e30
    RHSFactor = 1e30
    Notdamped = 20
    Iterations = 18
    ExitOnFailure
    SimStats
    ExtendedPrecision(80)
    CDensityMin = 1e-20
    TensorGridAniso(aniso)
    NumberofThreads = 8
    Method = Blocked
    SubMethod = ParDiSo
    Transient = BE
    CoordinateSystem { DFISE }
}

Plot {
    Potential SpaceCharge
    eDensity hDensity
    eCurrent/vector hCurrent/vector TotalCurrent/vector
    ElectricField/vector
    eQuasiFermi hQuasiFermi
    egradQuasiFermi hgradQuasiFermi
    SRH Auger
    eMobility hMobility
    eMobilityAniso hMobilityAniso
    eMobilityAnisoFactor hMobilityAnisoFactor
    DielectricConstant DielectricConstantAniso
    Doping DonorConcentration DonorPlusConcentration
    AcceptorConcentration AccepMinusConcentration
    NitrogenConcentration NitrogenActiveConcentration
    NitrogenConcentration_split1 NitrogenActiveConcentration_split1 NitrogenPlusConcentration_split1
    NitrogenConcentration_split2 NitrogenActiveConcentration_split2 NitrogenPlusConcentration_split2
    AluminumConcentration AluminumActiveConcentration
    ConductionBandEnergy ValenceBandEnergy BandGap
    #if @int@ > 0
    ComplexRefractiveIndex QuantumYield
    OpticalIntensity AbsorbedPhotonDensity OpticalGeneration
    #endif
}

System {
    * These two numerical devices share source, gate and drain circuit nodes.
    PPL_Device   ppl1   ("source"=0 "gate"=1 "drain"=2)
    NOPPL_Device noppl1 ("source"=0 "gate"=1 "drain"=2)

    Vsource_pset vg (1 0) { pwl=(0 0 100 15 200 15) }
    Vsource_pset vd (2 0) { pwl=(0 0 100 0 200 10) }

    Plot "n@node@_circuit.plt" (
        time() v(1) v(2)
        i(vd 2) i(ppl1 2) i(noppl1 2)
        i(vg 1) i(ppl1 1) i(noppl1 1)
    )
}

Solve {
    * Build the initial state of BOTH devices.
    Coupled(Iterations=1000 LineSearchDamping=1e-2) {
        ppl1.Poisson noppl1.Poisson
    }
    Coupled(Iterations=1000 LineSearchDamping=1e-2) {
        ppl1.Poisson ppl1.Electron ppl1.Contact
        noppl1.Poisson noppl1.Electron noppl1.Contact
        Circuit
    }
    Coupled(Iterations=1000 LineSearchDamping=1e-2) {
        ppl1.Poisson ppl1.Electron ppl1.Hole ppl1.Contact
        noppl1.Poisson noppl1.Electron noppl1.Hole noppl1.Contact
        Circuit
    }

#if @int@ > 0
    Quasistationary (
        InitialStep=1e-3 Increment=1.5 Decrement=2
        MinStep=1e-13 MaxStep=2e-2
        Goal { Device="ppl1" ModelParameter="Intensity" Value=@int@ }
        Goal { Device="noppl1" ModelParameter="Intensity" Value=@int@ }
    ) {
        Coupled { ppl1.Optics noppl1.Optics }
    }
#endif

    * Gate ramp at zero drain bias, including light turn-on.
    Transient (
        InitialTime=0 FinalTime=100
        InitialStep=1e-4 MinStep=1e-5 MaxStep=1
        Increment=1.5 Decrement=2.0
#if @int@ > 0
        TurningPoints (
            (Condition(Time(49.9; 50.0)) Value=0.01)
            (Condition(Time(Range=(49.9 50.1))) Value=0.01)
        )
#endif
    ) {
        Coupled {
            ppl1.Poisson ppl1.Electron ppl1.Hole ppl1.Contact
            noppl1.Poisson noppl1.Electron noppl1.Hole noppl1.Contact
            Circuit
        }
    }

#if @int@ > 0
    NewCurrentPrefix = "light_IdVd_"
#else
    NewCurrentPrefix = "dark_IdVd_"
#endif

    * Drain ramp: VDS=0.1*(t-100) V; VGS=15 V.
    Transient (
        InitialTime=100 FinalTime=200
        InitialStep=1e-4 MinStep=1e-5 MaxStep=5
        Increment=1.5 Decrement=2.0
    ) {
        Coupled {
            ppl1.Poisson ppl1.Electron ppl1.Hole ppl1.Contact
            noppl1.Poisson noppl1.Electron noppl1.Hole noppl1.Contact
            Circuit
        }
        CurrentPlot(Time=(Range=(100 200) Intervals=40))
    }
}
