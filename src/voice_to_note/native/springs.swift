// Vendored from MacPaw/CocoaSprings at commit
// 54ecdecad92c447d977a6623a03113f441b880e3 (https://github.com/MacPaw/CocoaSprings).
// The three Sources/Model files below are concatenated unchanged, followed by
// the + and - operators from Sources/Extensions/CGPoint+Extensions.swift.
// Platform wrappers and the extension's platform imports, NSValue conversion,
// and compound assignment operators are deliberately omitted.
// Exempt from house style: preserve upstream formatting and prose for comparison.

/*
MIT License

Copyright (c) 2023 MacPaw Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/

//
//  File.swift
//  
//
//  Created by Anton Barkov on 29.03.2023.
//

import Foundation

public struct SpringConfiguration {
    /// Controls how fast the spring motion oscillates.
    /// The higher the value, the faster the object moves towards equilibrium.
    public var angularFrequency: Float
    /// Controls how fast the spring motion decays.
    /// The lower the value, the less velocity is lost upon each oscillation. The value must range from 0 to 1.
    public var dampingRatio: Float

    public static let `default` = Self(angularFrequency: 7.5, dampingRatio: 0.5)
    
    public init(angularFrequency: Float, dampingRatio: Float) {
        self.angularFrequency = angularFrequency
        self.dampingRatio = dampingRatio
    }
}

//
//  SpringMotionState.swift
//  
//
//  Created by Anton Barkov on 05.04.2023.
//

import Foundation

final class SpringMotionState {
    
    struct Velocity {
        
        let horizontal: Double
        let vertical: Double
        
        var isZero: Bool {
            horizontal == 0 && vertical == 0
        }
        
        static let zero = Self(horizontal: 0, vertical: 0)
    }
    
    let position: CGPoint
    let velocity: Velocity
    
    init(position: CGPoint, velocity: Velocity) {
        self.position = position
        self.velocity = velocity
    }
}

//
//  SpringMotionPhysics.swift
//  
//
//  Created by Anton Barkov on 29.03.2023.
//

import Foundation

/// Implements the physics of spring motion.
///
/// The math is inspired by [this great post](https://www.ryanjuckett.com/damped-springs).
final class SpringMotionPhysics {
    
    private let posPosCoef: Double
    private let posVelCoef: Double
    private let velPosCoef: Double
    private let velVelCoef: Double
    
    init(configuration: SpringConfiguration, timeStep: Float = 0.001) {
        let c = configuration
        let omegaZeta = c.angularFrequency * c.dampingRatio
        let alpha = c.angularFrequency * sqrtf(1.0 - c.dampingRatio * c.dampingRatio)
        let expTerm = expf(-omegaZeta * timeStep)
        let cosTerm = cosf(alpha * timeStep)
        let sinTerm = sinf(alpha * timeStep)
        let invAlpha = 1.0 / alpha
        let expSin = expTerm * sinTerm
        let expCos = expTerm * cosTerm
        let expOmegaZetaSin_Over_Alpha = expTerm * omegaZeta * sinTerm * invAlpha
        
        posPosCoef = Double(expCos + expOmegaZetaSin_Over_Alpha)
        posVelCoef = Double(expSin * invAlpha)
        velPosCoef = Double(-expSin * alpha - omegaZeta * expOmegaZetaSin_Over_Alpha)
        velVelCoef = Double(expCos - expOmegaZetaSin_Over_Alpha)
    }
    
    func calculateNextState(
        from state: SpringMotionState,
        destinationPoint: CGPoint
    ) -> SpringMotionState {
        let relPos = state.position - destinationPoint
        let horVelCoef = state.velocity.horizontal * posVelCoef
        let verVelCoef = state.velocity.vertical * posVelCoef
        let xCoef = relPos.x * posPosCoef
        let yCoef = relPos.y * posPosCoef
        
        return SpringMotionState(
            position: .init(x: xCoef + horVelCoef + destinationPoint.x,
                            y: yCoef + verVelCoef + destinationPoint.y),
            velocity: .init(horizontal: (relPos.x * velPosCoef) + (state.velocity.horizontal * velVelCoef),
                            vertical: (relPos.y * velPosCoef) + (state.velocity.vertical * velVelCoef))
        )
    }
    
    func calculateAllStates(
        from initialState: SpringMotionState,
        destinationPoint: CGPoint
    ) -> [SpringMotionState] {
        
        var currentState = initialState
        var allStates = [SpringMotionState]()
        
        var shouldContinue = true
        while shouldContinue {
            let nextState = calculateNextState(from: currentState, destinationPoint: destinationPoint)
            
            guard abs(nextState.velocity.horizontal) > 0.001 || abs(nextState.velocity.vertical) > 0.001 else {
                shouldContinue = false
                continue
            }
            
            allStates.append(nextState)
            currentState = nextState
        }
        
        return allStates
    }
}

//
//  File.swift
//  
//
//  Created by Anton Barkov on 31.03.2023.
//

extension CGPoint {
    
    static func + (left: CGPoint, right: CGPoint) -> CGPoint {
        CGPoint(x: left.x + right.x, y: left.y + right.y)
    }

    static func - (left: CGPoint, right: CGPoint) -> CGPoint {
        CGPoint(x: left.x - right.x, y: left.y - right.y)
    }

}
